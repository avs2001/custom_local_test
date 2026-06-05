"""Unit tests for the heart-rate fitness-age handler.

Covers:
- ``parse_hr_csv``  — happy path, empty, malformed, missing column.
- ``compute_fitness_age`` — sanity bounds + male/female differential.
- The async ``fitness_age_handler`` with mocked ``ctx.blob``.

No broker, no Azurite, no SDK runtime required.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest

# main.py lives one directory up; insert it onto sys.path so we can import
# the handler module directly (the demo repo is not a package).
DEMO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEMO_ROOT))
sys.path.insert(0, str(DEMO_ROOT / "samples"))
sys.path.insert(0, str(DEMO_ROOT / "scripts"))

import main as demo_main  # noqa: E402
import generate as gen  # noqa: E402
import send_sample as sender  # noqa: E402
from kbm_ledsas_sdk import errors  # noqa: E402

SAMPLES_DIR = DEMO_ROOT / "samples"


# ---------------------------------------------------------------------------
# parse_hr_csv
# ---------------------------------------------------------------------------


def _synthetic_csv(n: int = 100, mean: float = 70.0, sd: float = 10.0, seed: int = 7) -> str:
    rng = np.random.default_rng(seed)
    samples = rng.normal(mean, sd, n).clip(40, 200).round().astype(int)
    buf = io.StringIO()
    buf.write("timestamp_iso,heart_rate_bpm\n")
    for i, s in enumerate(samples):
        buf.write(f"2026-05-28T09:{i // 60:02d}:{i % 60:02d}Z,{s}\n")
    return buf.getvalue()


def test_parse_hr_csv_happy_path() -> None:
    csv = _synthetic_csv(n=50)
    df = demo_main.parse_hr_csv(csv)
    assert len(df) == 50
    assert "heart_rate_bpm" in df.columns
    assert df["heart_rate_bpm"].between(40, 200).all()


def test_parse_hr_csv_empty_raises() -> None:
    with pytest.raises(ValueError):
        demo_main.parse_hr_csv("")


def test_parse_hr_csv_whitespace_only_raises() -> None:
    with pytest.raises(ValueError):
        demo_main.parse_hr_csv("   \n  \n")


def test_parse_hr_csv_missing_column_raises() -> None:
    with pytest.raises(ValueError, match="heart_rate_bpm"):
        demo_main.parse_hr_csv("timestamp_iso,foo\n2026-05-28T09:00:00Z,bar\n")


def test_parse_hr_csv_all_nonnumeric_raises() -> None:
    csv = "timestamp_iso,heart_rate_bpm\n2026-05-28T09:00:00Z,xx\n2026-05-28T09:00:01Z,yy\n"
    with pytest.raises(ValueError):
        demo_main.parse_hr_csv(csv)


# ---------------------------------------------------------------------------
# compute_fitness_age
# ---------------------------------------------------------------------------


def _df(samples: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"heart_rate_bpm": samples})


def test_compute_fitness_age_returns_expected_keys() -> None:
    df = _df([60, 65, 70, 75, 80, 85, 90])
    out = demo_main.compute_fitness_age(df, {"age": 40, "sex": "male"})
    assert set(out.keys()) == {
        "resting_hr_bpm",
        "avg_hr_bpm",
        "max_hr_predicted",
        "vo2_max_estimated",
        "fitness_age",
    }
    assert 20 <= out["fitness_age"] <= 90
    assert out["max_hr_predicted"] == pytest.approx(208 - 0.7 * 40, rel=1e-6)


def test_compute_fitness_age_in_clamp_range() -> None:
    # A fit 30-year-old with low resting HR should land low; bounds still hold.
    df = _df([55] * 100)
    out = demo_main.compute_fitness_age(df, {"age": 30, "sex": "male"})
    assert 20 <= out["fitness_age"] <= 90


def test_compute_fitness_age_male_vs_female_differs() -> None:
    # Higher resting HR -> lower VO2max -> result inside the [20,90] clamp window
    # where the male/female coefficients actually diverge.
    df = _df([90, 95, 100, 105, 110])
    male = demo_main.compute_fitness_age(df, {"age": 60, "sex": "male"})
    female = demo_main.compute_fitness_age(df, {"age": 60, "sex": "female"})
    # Same inputs, different coefficients => different result.
    assert male["fitness_age"] != female["fitness_age"]
    # vo2_max is sex-independent; only the fitness-age fit differs.
    assert male["vo2_max_estimated"] == female["vo2_max_estimated"]


def test_compute_fitness_age_age_matters() -> None:
    df = _df([60, 65, 70, 75, 80])
    young = demo_main.compute_fitness_age(df, {"age": 25, "sex": "male"})
    old = demo_main.compute_fitness_age(df, {"age": 65, "sex": "male"})
    # Older subject has lower max_hr_predicted -> lower vo2_max -> higher fitness_age.
    assert old["fitness_age"] >= young["fitness_age"]


# ---------------------------------------------------------------------------
# fitness_age_handler (async, with mocked ctx)
# ---------------------------------------------------------------------------


def _make_ctx(csv_text: str, idempotency_key: str = "demo-run-001"):
    """Build a minimal ctx mock matching the real ExecutionContext surface."""
    blob_ops = SimpleNamespace(
        download_text=AsyncMock(return_value=csv_text),
        upload_json=AsyncMock(
            return_value=SimpleNamespace(
                uri=f"azblob://heart-rate-fitness-age/fitness-age/{idempotency_key}.json"
            )
        ),
    )
    return (
        SimpleNamespace(blob=blob_ops, idempotency_key=idempotency_key),
        blob_ops,
    )


def _run(coro):
    return asyncio.run(coro)


def test_handler_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KBM_LEDSAS_CONTAINER", "heart-rate-fitness-age")
    csv = _synthetic_csv(n=100, mean=68, sd=8, seed=1)
    ctx, blob_ops = _make_ctx(csv)
    req = {
        "demographics": {"age": 42, "sex": "male", "weight_kg": 80, "height_cm": 178},
        "input_blob": "azblob://heart-rate-fitness-age/samples/run-001.csv",
    }

    result = _run(demo_main.fitness_age_handler(ctx, req))

    assert result["status"] == "ok"
    assert "fitness-age/" in result["output_blob"]
    assert 20 <= result["fitness_age"] <= 90
    assert isinstance(result["vo2_max_estimated"], float)

    blob_ops.download_text.assert_awaited_once_with(req["input_blob"])
    upload_call = blob_ops.upload_json.await_args
    assert upload_call.kwargs["container"] == "heart-rate-fitness-age"
    assert upload_call.kwargs["overwrite"] is True
    assert upload_call.kwargs["path"].startswith("fitness-age/")
    written = upload_call.kwargs["obj"]
    assert "fitness_age" in written and "vo2_max_estimated" in written
    assert written["demographics"] == req["demographics"]


def test_handler_missing_input_blob_raises_permanent() -> None:
    ctx, _ = _make_ctx("")
    req = {"demographics": {"age": 42, "sex": "male"}}
    with pytest.raises(errors.Permanent):
        _run(demo_main.fitness_age_handler(ctx, req))


def test_handler_missing_demographics_raises_permanent() -> None:
    ctx, _ = _make_ctx("")
    req = {"input_blob": "azblob://x/y.csv"}
    with pytest.raises(errors.Permanent):
        _run(demo_main.fitness_age_handler(ctx, req))


def test_handler_empty_csv_raises_permanent() -> None:
    ctx, _ = _make_ctx("")
    req = {
        "demographics": {"age": 42, "sex": "male"},
        "input_blob": "azblob://x/y.csv",
    }
    with pytest.raises(errors.Permanent, match="Invalid heart-rate CSV"):
        _run(demo_main.fitness_age_handler(ctx, req))


def test_handler_malformed_csv_raises_permanent() -> None:
    ctx, _ = _make_ctx("not,a,real,csv\n\x00garbage\x00\n")
    req = {
        "demographics": {"age": 42, "sex": "male"},
        "input_blob": "azblob://x/y.csv",
    }
    with pytest.raises(errors.Permanent, match="Invalid heart-rate CSV"):
        _run(demo_main.fitness_age_handler(ctx, req))


def test_handler_different_sex_gives_different_result() -> None:
    # Higher mean HR (less-fit subject) pushes the result inside the
    # [20, 90] clamp where male and female coefficients actually diverge.
    csv = _synthetic_csv(n=100, mean=100, sd=5, seed=42)
    ctx_m, _ = _make_ctx(csv)
    ctx_f, _ = _make_ctx(csv)
    req_m = {
        "demographics": {"age": 60, "sex": "male"},
        "input_blob": "azblob://x/y.csv",
    }
    req_f = {**req_m, "demographics": {"age": 60, "sex": "female"}}
    out_m = _run(demo_main.fitness_age_handler(ctx_m, req_m))
    out_f = _run(demo_main.fitness_age_handler(ctx_f, req_f))
    assert out_m["fitness_age"] != out_f["fitness_age"]


# ---------------------------------------------------------------------------
# Committed sample CSVs (samples/*.csv) end-to-end through the pure helpers
# ---------------------------------------------------------------------------


def _fitness_age_for_sample(scenario: "gen.Scenario") -> int:
    csv_text = (SAMPLES_DIR / scenario.filename).read_text()
    df = demo_main.parse_hr_csv(csv_text)
    out = demo_main.compute_fitness_age(
        df, {"age": scenario.age, "sex": scenario.sex}
    )
    return out["fitness_age"]


def test_all_committed_samples_parse_and_score_in_range() -> None:
    """Each committed sample must parse (1440 rows) and score in [20, 90]."""
    assert len(gen.SCENARIOS) == 5
    for sc in gen.SCENARIOS:
        path = SAMPLES_DIR / sc.filename
        assert path.is_file(), f"missing committed sample: {path}"
        df = demo_main.parse_hr_csv(path.read_text())
        assert len(df) == gen.SAMPLES_PER_DAY  # 1440 rows
        out = demo_main.compute_fitness_age(
            df, {"age": sc.age, "sex": sc.sex}
        )
        assert 20 <= out["fitness_age"] <= 90, (sc.filename, out)


def test_committed_samples_ordering_is_sane() -> None:
    """Fitter scenarios must score younger than their less-fit peers."""
    fa = {sc.filename: _fitness_age_for_sample(sc) for sc in gen.SCENARIOS}
    # Athlete is the fittest -> youngest fitness age overall.
    assert fa["young_athlete.csv"] == min(fa.values())
    # Active subjects younger than the sedentary subject in the same age band.
    assert fa["midlife_active.csv"] < fa["midlife_sedentary.csv"]
    assert fa["older_active.csv"] < fa["older_sedentary.csv"]
    # Older sedentary is the least fit -> oldest fitness age overall.
    assert fa["older_sedentary.csv"] == max(fa.values())


def test_generator_is_deterministic() -> None:
    """Same seed => identical series for a scenario (re-seedable contract)."""
    sc = gen.SCENARIOS[0]
    a = gen.generate_series(sc, base_seed=0)
    b = gen.generate_series(sc, base_seed=0)
    assert (a == b).all()
    assert len(a) == gen.SAMPLES_PER_DAY
    # A different base seed yields a different series.
    c = gen.generate_series(sc, base_seed=12345)
    assert not (a == c).all()


def test_committed_samples_match_generator_output(tmp_path: Path) -> None:
    """The committed CSVs must equal a fresh seed-0 regeneration (byte-exact)."""
    gen.generate_all(base_seed=0, out_dir=tmp_path)
    for sc in gen.SCENARIOS:
        committed = (SAMPLES_DIR / sc.filename).read_bytes()
        fresh = (tmp_path / sc.filename).read_bytes()
        assert committed == fresh, f"{sc.filename} drifted from generator output"


# ---------------------------------------------------------------------------
# send_sample.py helpers (no broker / no Azure required)
# ---------------------------------------------------------------------------


def test_command_exchange_name_matches_sdk_topology() -> None:
    # Mirrors kbm_ledsas_sdk.amqp.topology.build_exchange_name("cmd", ...).
    from kbm_ledsas_sdk.amqp.topology import build_exchange_name

    expected = build_exchange_name("cmd", "demo", "HeartRateFitnessAge")
    assert expected == "cmd.demo.HeartRateFitnessAge.v1"
    assert (
        sender.build_command_exchange_name("demo", "HeartRateFitnessAge")
        == expected
    )
    # Without tenant.
    assert (
        sender.build_command_exchange_name(None, "HeartRateFitnessAge")
        == build_exchange_name("cmd", None, "HeartRateFitnessAge")
    )


def test_demographics_for_each_sample() -> None:
    for sc in gen.SCENARIOS:
        demo = sender.demographics_for(sc.filename)
        assert demo["age"] == sc.age
        assert demo["sex"] == sc.sex


def test_demographics_for_unknown_raises() -> None:
    with pytest.raises(KeyError):
        sender.demographics_for("does_not_exist.csv")


def test_build_idempotency_key_shape() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    key = sender.build_idempotency_key("samples/young_athlete.csv", now=now)
    assert key == "demo-young_athlete-20260102T030405Z"


def test_build_command_message_shape() -> None:
    msg = sender.build_command_message(
        handler_name="fitness_age",
        reply_to="reply-ex-hr-abc123",
        idempotency_key="demo-young_athlete-20260102T030405Z",
        input_blob="azblob://heart-rate-fitness-age/samples/run.csv",
        demographics={"age": 28, "sex": "male"},
        correlation_id="corr-123",
    )
    env = msg["envelope"]
    assert env["type"] == "command"
    assert env["name"] == "fitness_age"
    assert env["reply_to"] == "reply-ex-hr-abc123"
    assert env["correlation_id"] == "corr-123"
    assert env["idempotency_key"] == "demo-young_athlete-20260102T030405Z"
    assert env["schema_version"] == "1.0"
    # message_id / trace_id auto-filled with UUIDs.
    assert env["message_id"] and env["trace_id"]
    payload = msg["payload"]
    assert payload["input_blob"].startswith("azblob://")
    assert payload["demographics"] == {"age": 28, "sex": "male"}


def test_parse_blob_uri_roundtrip() -> None:
    container, blob = sender.parse_blob_uri(
        "azblob://heart-rate-fitness-age/samples/run-001.csv"
    )
    assert container == "heart-rate-fitness-age"
    assert blob == "samples/run-001.csv"


def test_parse_blob_uri_rejects_bad_uri() -> None:
    with pytest.raises(ValueError):
        sender.parse_blob_uri("https://example.com/x")
    with pytest.raises(ValueError):
        sender.parse_blob_uri("azblob://only-container")


def test_env_defaults_and_exchange() -> None:
    env = sender.Env(environ={})  # no env vars -> documented defaults
    assert env.service_name == "HeartRateFitnessAge"
    assert env.tenant == "demo"
    assert env.command_exchange == "cmd.demo.HeartRateFitnessAge.v1"
    assert env.container == "heart-rate-fitness-age"


def test_env_overrides() -> None:
    env = sender.Env(
        environ={
            "KBM_LEDSAS_SERVICE_NAME": "Svc",
            "KBM_LEDSAS_TENANT": "acme",
            "KBM_LEDSAS_CONTAINER": "out",
        }
    )
    assert env.command_exchange == "cmd.acme.Svc.v1"
    assert env.container == "out"


def test_send_sample_envelope_drives_real_handler() -> None:
    """Integration of sender + handler with an in-memory transport (no broker).

    The sender builds a command message; we feed its payload straight into the
    real ``fitness_age_handler`` with a mocked ctx whose blob download returns a
    committed sample CSV. This exercises the envelope/demographics wiring end to
    end without RabbitMQ or Azurite.
    """
    sample = "young_athlete.csv"
    csv_text = (SAMPLES_DIR / sample).read_text()
    demo = sender.demographics_for(sample)
    idem = sender.build_idempotency_key(sample)
    msg = sender.build_command_message(
        handler_name="fitness_age",
        reply_to="reply-ex-hr-test",
        idempotency_key=idem,
        input_blob=sender.build_input_blob_uri("heart-rate-fitness-age", idem),
        demographics=demo,
    )
    ctx, blob_ops = _make_ctx(csv_text, idempotency_key=idem)
    result = _run(demo_main.fitness_age_handler(ctx, msg["payload"]))
    assert result["status"] == "ok"
    assert 20 <= result["fitness_age"] <= 90
    blob_ops.download_text.assert_awaited_once_with(msg["payload"]["input_blob"])
