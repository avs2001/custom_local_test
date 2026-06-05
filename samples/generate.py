#!/usr/bin/env python3
"""Synthetic heart-rate sample generator for the LEDSAS fitness-age demo.

This module produces **purely synthetic** 24-hour heart-rate recordings at
1-minute sampling (1440 rows). No real subject data is used or implied. The
statistical model is a transparent composition of four published-physiology
building blocks:

1. **Resting baseline** — a per-scenario constant resting heart rate. Fitter
   subjects have lower resting HR; this is the dominant driver of the
   fitness-age estimate (the demo's ``compute_fitness_age`` keys VO2max off
   the 5th-percentile resting HR).

2. **Circadian sinusoid** — a slow diurnal rhythm (24-hour period) that lifts
   HR during waking hours and lowers it overnight. Amplitude is per-scenario.

3. **Activity bouts** — Gaussian "bumps" centred on per-scenario exercise
   windows (e.g. a morning run, an evening workout). During a bout, HR is
   driven toward a fraction of the age-predicted maximum.

4. **AR(1) heart-rate variability** — a first-order autoregressive noise term
   (``x[t] = phi * x[t-1] + eps``) that gives the realistic minute-to-minute
   wander of beat-rate without unphysical white-noise jumps.

The age-predicted maximum heart rate uses the Tanaka 2001 formula
``HRmax = 208 - 0.7 * age`` (Tanaka H, Monahan KD, Seals DR. "Age-predicted
maximal heart rate revisited." J Am Coll Cardiol. 2001;37(1):153-156). The
per-scenario baseline / amplitude / bout choices are tuned so that the demo's
Nes-Wisloff inverse-fit fitness-age estimate (Nes BM, Janszky I, Wisloff U,
et al. "Age-predicted maximal heart rate in healthy subjects: The HUNT
fitness study." Scand J Med Sci Sports. 2013;23(6):697-704; and the
VO2max/fitness-age work from the same Norwegian HUNT cohort) lands in a
plausible, scenario-appropriate range (athletes younger than chronological
age, sedentary subjects older).

Determinism
-----------
The seed for each scenario is derived from its name via a stable hash, so
re-running ``generate.py`` produces **byte-identical** CSVs. Pass ``--seed``
to override the base seed for all scenarios (useful for generating an
alternate sample set with the same statistics).

Output schema
-------------
Each CSV has exactly two columns matching what the demo handler's
``parse_hr_csv`` expects::

    timestamp_iso,heart_rate_bpm
    2026-01-01T00:00:00Z,49
    2026-01-01T00:01:00Z,50
    ...

Usage
-----
    python samples/generate.py                 # regenerate all 5 scenarios
    python samples/generate.py --seed 1234      # alternate seed for all
    python samples/generate.py --list           # list scenarios + demographics

Only numpy + the standard library are used (numpy is already a demo
requirement).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

import numpy as np

# Sampling grid: 24 hours at 1-minute resolution.
SAMPLES_PER_DAY = 24 * 60  # 1440
SAMPLE_PERIOD_S = 60
# Fixed start instant so timestamps are deterministic across runs.
START_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
# Physiological clamps for the final integer BPM series.
HR_FLOOR = 35
HR_CEIL = 200


@dataclass(frozen=True)
class Bout:
    """A Gaussian activity bout.

    Attributes:
        center_hour: Hour-of-day (0-24, may be fractional) the bout peaks.
        width_min: Standard deviation of the Gaussian, in minutes.
        intensity: Fraction of (HRmax - baseline) the peak adds on top of
            the circadian-modulated baseline (0..1).
    """

    center_hour: float
    width_min: float
    intensity: float


@dataclass(frozen=True)
class Scenario:
    """Parameters for one synthetic recording.

    The demographics travel with the scenario so that ``send_sample.py`` and
    the generator agree on age/sex/weight/height for each CSV.
    """

    filename: str
    age: int
    sex: str  # "male" | "female"
    weight_kg: float
    height_cm: float
    baseline_bpm: float
    circadian_amplitude: float
    ar1_sigma: float
    bouts: Tuple[Bout, ...] = field(default_factory=tuple)
    ar1_phi: float = 0.85  # AR(1) memory; shared across scenarios.
    # Per-scenario calibration shift (bpm) applied uniformly to the whole
    # series. The plan's baseline_bpm values are physiologically realistic
    # *nocturnal* resting rates (48-80). The demo handler's compute_fitness_age
    # recovers "resting HR" as the 5th percentile of the WHOLE day and feeds it
    # through vo2 = HRmax/resting*15.3 + a Nes-Wisloff inverse fit whose
    # meaningful fitness-age band [20,90] corresponds to a 5th-percentile HR of
    # roughly 70-130 bpm. Without this shift, every realistic resting HR maps
    # to the clamp floor (fitness_age = 20) and the scenarios are
    # indistinguishable. The offset lifts each scenario's distribution so the
    # *handler's* output spreads across [20,90] with athletes youngest and
    # sedentary subjects oldest. See README "Calibration note" + the ST-1
    # deviation note. baseline/amplitude/bouts/AR-sigma are unchanged from the
    # plan; only this additive calibration constant is per-scenario tuned.
    resting_offset_bpm: float = 0.0

    @property
    def hr_max(self) -> float:
        """Tanaka 2001 age-predicted maximum heart rate."""
        return 208.0 - 0.7 * self.age


# ---------------------------------------------------------------------------
# Scenario catalogue — parameters per the TASK-028 plan, verbatim.
# ---------------------------------------------------------------------------

SCENARIOS: Tuple[Scenario, ...] = (
    Scenario(
        filename="young_athlete.csv",
        age=28,
        sex="male",
        weight_kg=72.0,
        height_cm=180.0,
        baseline_bpm=48.0,
        circadian_amplitude=8.0,
        ar1_sigma=4.0,
        resting_offset_bpm=34.0,
        bouts=(
            Bout(center_hour=7.5, width_min=30.0, intensity=0.80),
            Bout(center_hour=17.5, width_min=30.0, intensity=0.80),
        ),
    ),
    Scenario(
        filename="midlife_active.csv",
        age=42,
        sex="female",
        weight_kg=64.0,
        height_cm=166.0,
        baseline_bpm=62.0,
        circadian_amplitude=10.0,
        ar1_sigma=5.0,
        resting_offset_bpm=30.0,
        bouts=(
            Bout(center_hour=6.5, width_min=30.0, intensity=0.70),
            Bout(center_hour=18.5, width_min=30.0, intensity=0.70),
        ),
    ),
    Scenario(
        filename="midlife_sedentary.csv",
        age=45,
        sex="male",
        weight_kg=92.0,
        height_cm=176.0,
        baseline_bpm=74.0,
        circadian_amplitude=12.0,
        ar1_sigma=6.0,
        resting_offset_bpm=28.0,
        bouts=(
            Bout(center_hour=12.0, width_min=20.0, intensity=0.35),
            Bout(center_hour=19.0, width_min=20.0, intensity=0.30),
        ),
    ),
    Scenario(
        filename="older_active.csv",
        age=65,
        sex="female",
        weight_kg=66.0,
        height_cm=162.0,
        baseline_bpm=58.0,
        circadian_amplitude=8.0,
        ar1_sigma=4.0,
        resting_offset_bpm=36.0,
        bouts=(
            Bout(center_hour=7.5, width_min=30.0, intensity=0.65),
            Bout(center_hour=16.5, width_min=30.0, intensity=0.65),
        ),
    ),
    Scenario(
        filename="older_sedentary.csv",
        age=68,
        sex="male",
        weight_kg=88.0,
        height_cm=174.0,
        baseline_bpm=80.0,
        circadian_amplitude=12.0,
        ar1_sigma=6.0,
        resting_offset_bpm=38.0,
        bouts=(
            Bout(center_hour=15.0, width_min=15.0, intensity=0.20),
        ),
    ),
)


def scenario_by_filename(name: str) -> Scenario:
    """Look up a scenario by its CSV filename (with or without dir prefix)."""
    stem = Path(name).name
    for sc in SCENARIOS:
        if sc.filename == stem:
            return sc
    raise KeyError(
        f"unknown scenario {name!r}; known: {[s.filename for s in SCENARIOS]}"
    )


def _seed_for(scenario_name: str, base_seed: int) -> int:
    """Derive a stable 32-bit seed from the scenario name + base seed.

    Uses BLAKE2b so the mapping is reproducible across Python runs and
    platforms (Python's built-in ``hash`` is salted per-process).
    """
    h = hashlib.blake2b(
        scenario_name.encode("utf-8"), digest_size=8, person=b"ledsas-hr"
    )
    name_hash = int.from_bytes(h.digest(), "big")
    return (name_hash ^ (base_seed & 0xFFFFFFFF)) & 0x7FFFFFFF


def generate_series(scenario: Scenario, base_seed: int) -> np.ndarray:
    """Generate the 1440-sample integer BPM series for a scenario.

    Returns:
        ``np.ndarray`` of dtype int, length ``SAMPLES_PER_DAY``.
    """
    rng = np.random.default_rng(_seed_for(scenario.filename, base_seed))

    minutes = np.arange(SAMPLES_PER_DAY, dtype=float)
    hours = minutes / 60.0

    # 1. Resting baseline. This is the overnight/at-rest level; the demo's
    #    ``compute_fitness_age`` recovers it as the 5th-percentile of the day,
    #    so the circadian term below is deliberately a non-negative *daytime
    #    elevation* (HR rises while awake, settles back to baseline asleep)
    #    rather than a symmetric sinusoid that would dip the 5th percentile
    #    artificially below the true resting rate. The per-scenario
    #    calibration offset (see Scenario.resting_offset_bpm) is folded into
    #    the baseline here.
    series = np.full(
        SAMPLES_PER_DAY,
        scenario.baseline_bpm + scenario.resting_offset_bpm,
        dtype=float,
    )

    # 2. Circadian elevation: a raised cosine that is ~0 overnight (trough near
    #    04:00) and peaks in the afternoon (~16:00). Range [0, amplitude].
    circadian = scenario.circadian_amplitude * 0.5 * (
        1.0 - np.cos(2.0 * np.pi * (hours - 4.0) / 24.0)
    )
    series += circadian

    # 3. Activity bouts: Gaussian bumps toward HRmax (from the calibrated
    #    baseline so bouts never overshoot the age-predicted maximum).
    headroom = max(0.0, scenario.hr_max - (scenario.baseline_bpm + scenario.resting_offset_bpm))
    for bout in scenario.bouts:
        center_min = bout.center_hour * 60.0
        gauss = np.exp(
            -0.5 * ((minutes - center_min) / bout.width_min) ** 2
        )
        series += bout.intensity * headroom * gauss

    # 4. AR(1) HRV noise.
    eps = rng.normal(0.0, scenario.ar1_sigma, SAMPLES_PER_DAY)
    ar = np.empty(SAMPLES_PER_DAY, dtype=float)
    ar[0] = eps[0]
    phi = scenario.ar1_phi
    for t in range(1, SAMPLES_PER_DAY):
        ar[t] = phi * ar[t - 1] + eps[t]
    series += ar

    # Clamp and quantise to integer BPM.
    series = np.clip(np.round(series), HR_FLOOR, HR_CEIL).astype(int)
    return series


def write_csv(scenario: Scenario, base_seed: int, out_dir: Path) -> Path:
    """Generate and write one scenario CSV. Returns the written path."""
    series = generate_series(scenario, base_seed)
    out_path = out_dir / scenario.filename
    # newline="" + explicit "\n" lineterminator => identical bytes on all OSes.
    with out_path.open("w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["timestamp_iso", "heart_rate_bpm"])
        ts = START_TIME
        for bpm in series:
            # e.g. 2026-01-01T00:00:00Z
            writer.writerow(
                [ts.strftime("%Y-%m-%dT%H:%M:%SZ"), int(bpm)]
            )
            ts += timedelta(seconds=SAMPLE_PERIOD_S)
    return out_path


def generate_all(base_seed: int, out_dir: Path) -> List[Path]:
    """Generate all scenarios into ``out_dir``. Returns written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return [write_csv(sc, base_seed, out_dir) for sc in SCENARIOS]


def _main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate synthetic heart-rate CSV samples for the "
        "LEDSAS fitness-age demo."
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Base seed XORed into each scenario's name-derived seed "
        "(default 0). Same seed => byte-identical CSVs.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Output directory (default: this samples/ directory).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List scenarios + demographics and exit.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for sc in SCENARIOS:
            print(
                f"{sc.filename:24s} age={sc.age:>2d} sex={sc.sex:<6s} "
                f"baseline={sc.baseline_bpm:>4.0f}bpm "
                f"HRmax={sc.hr_max:>5.1f} bouts={len(sc.bouts)}"
            )
        return 0

    paths = generate_all(args.seed, args.out)
    for p in paths:
        # Count data rows (excluding header).
        n = sum(1 for _ in p.open()) - 1
        print(f"wrote {p}  ({n} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
