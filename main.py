"""
Heart-rate fitness-age service.

Consumes a CSV blob of heart-rate samples + demographics, computes a
fitness-age estimate using published cohort approximations, and writes
the result to an output blob.

Handler: ``fitness_age``
Input shape::

    {
        "demographics": {"age": int, "sex": "male"|"female",
                         "weight_kg": float, "height_cm": float},
        "input_blob":  "azblob://.../samples.csv"
    }

Output shape::

    {
        "status": "ok",
        "output_blob": "azblob://.../fitness-age/<idempotency_key>.json",
        "fitness_age": int,
        "vo2_max_estimated": float (1 decimal)
    }

Errors:
- Bad / empty / unparseable CSV  -> ``Permanent`` (DLQ, no retry)
- Transient blob storage failures -> SDK re-raises ``Retryable`` (auto-retry)

NOTE: the fitness-age formula here is a simplified public-knowledge
approximation (Nes/Wisløff cohort). Production deployments should
swap in a validated model. See README §"What this demo does NOT do".
"""

from __future__ import annotations

import io
import os
from typing import Any, Dict

import pandas as pd

from kbm_ledsas_sdk import ServiceApp, errors


app = ServiceApp(service_name="HeartRateFitnessAge")


# ---------------------------------------------------------------------------
# Pure compute helpers — broken out from the handler so they are trivially
# unit-testable without spinning up the SDK runtime or mocking blob I/O.
# ---------------------------------------------------------------------------


def parse_hr_csv(csv_text: str) -> pd.DataFrame:
    """Parse a heart-rate CSV into a DataFrame.

    Expected columns: ``timestamp_iso,heart_rate_bpm``.

    Raises:
        ValueError: when the CSV is empty, malformed, or missing columns.
    """
    if not csv_text or not csv_text.strip():
        raise ValueError("empty CSV")
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except pd.errors.EmptyDataError as e:
        raise ValueError(f"empty CSV: {e}") from e
    except Exception as e:  # noqa: BLE001 — any pandas parse error is a bad input
        raise ValueError(f"unparseable CSV: {e}") from e

    if "heart_rate_bpm" not in df.columns:
        raise ValueError("missing required column: heart_rate_bpm")

    df["heart_rate_bpm"] = pd.to_numeric(df["heart_rate_bpm"], errors="coerce")
    df = df.dropna(subset=["heart_rate_bpm"])
    if df.empty:
        raise ValueError("no valid heart-rate samples after parsing")
    return df


def compute_fitness_age(df: pd.DataFrame, demographics: Dict[str, Any]) -> Dict[str, Any]:
    """Compute fitness-age metrics from a heart-rate sample DataFrame.

    Returns a dict with keys: ``resting_hr_bpm``, ``avg_hr_bpm``,
    ``max_hr_predicted``, ``vo2_max_estimated``, ``fitness_age``.
    """
    age = int(demographics["age"])
    sex = str(demographics["sex"]).lower()

    hr = df["heart_rate_bpm"]
    resting_hr_bpm = float(hr.quantile(0.05))
    avg_hr_bpm = float(hr.mean())
    max_hr_predicted = 208.0 - 0.7 * age

    # Defensive: a degenerate resting_hr_bpm of 0 would explode the ratio.
    if resting_hr_bpm <= 0:
        raise ValueError(f"resting_hr_bpm out of range: {resting_hr_bpm}")

    vo2_max_estimated = (max_hr_predicted / resting_hr_bpm) * 15.3

    # Nes/Wisløff inverse-fit approximations (public knowledge).
    if sex == "female":
        raw = 105.91 - 2.20 * vo2_max_estimated
    else:
        raw = 108.42 - 2.05 * vo2_max_estimated
    fitness_age = max(20, min(90, round(raw)))

    return {
        "resting_hr_bpm": round(resting_hr_bpm, 1),
        "avg_hr_bpm": round(avg_hr_bpm, 1),
        "max_hr_predicted": round(max_hr_predicted, 1),
        "vo2_max_estimated": round(vo2_max_estimated, 1),
        "fitness_age": int(fitness_age),
    }


@app.handler("fitness_age")
async def fitness_age_handler(ctx, req: Dict[str, Any]) -> Dict[str, Any]:
    demographics = req.get("demographics") or {}
    input_blob = req.get("input_blob")
    if not input_blob:
        raise errors.Permanent("Invalid request: 'input_blob' is required")
    if not demographics.get("age") or not demographics.get("sex"):
        raise errors.Permanent("Invalid request: demographics.age and demographics.sex are required")

    # Download the CSV. Retryable blob errors propagate (SDK auto-retries).
    csv_text = await ctx.blob.download_text(input_blob)

    try:
        df = parse_hr_csv(csv_text)
        metrics = compute_fitness_age(df, demographics)
    except (pd.errors.EmptyDataError, ValueError, KeyError) as e:
        raise errors.Permanent(f"Invalid heart-rate CSV: {e}") from e

    container = os.getenv("KBM_LEDSAS_CONTAINER", "dev")
    result = {
        "demographics": demographics,
        "input_blob": input_blob,
        **metrics,
    }
    # Key the output path on ctx.idempotency_key (stable across DLQ replays),
    # NOT ctx.message_id (changes per send). overwrite=True is required so a
    # retried message rewrites the same blob instead of failing with
    # BlobAlreadyExists. See SDK_API_REFERENCE.md §ExecutionContext.
    output_ref = await ctx.blob.upload_json(
        container=container,
        obj=result,
        path=f"fitness-age/{ctx.idempotency_key}.json",
        overwrite=True,
    )

    return {
        "status": "ok",
        "output_blob": output_ref.uri,
        "fitness_age": metrics["fitness_age"],
        "vo2_max_estimated": metrics["vo2_max_estimated"],
    }


if __name__ == "__main__":
    app.run()
