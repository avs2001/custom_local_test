# Processor v7.0.0 — Summary of Changes for Kubyk / LEDSAS

## What changed and why

### 1. Input CSV: 12 → 15 columns (V7 hardware)

The V7 device firmware now sends 3 additional fields. The processor has been
updated to accept the new 15-column format:

| New column | Position | Type | Description |
|---|---|---|---|
| SensorID | col 2 | alphanumeric | Physical UN sensor ID (e.g. `SN-001`) |
| DTemp3 | col 7 | numeric | Third device temperature sensor (°C) |
| Status | col 14 | numeric | Sensor status from firmware (0 = OK) |

Full column order (V7):
```
Device SN | PatientID | SensorID | DateStamp | TimeStamp |
DTemp1 | DTemp2 | DTemp3 | ATemp |
VNa | VK | VNaK | VpH | EC | Status
```

---

### 2. PatientID: numeric → alphanumeric NFC tag

| | v6 | v7 |
|---|---|---|
| PatientID format | integer | alphanumeric string (e.g. `NFC-A3B9`) |
| Calibration session | PatientID < 0 | PatientID == `"CAL"` |

---

### 3. EC flat / dry run — no longer an error

If EC signal is flat (all-zero or all-same), the processor now:
- Uses the last 1/3 of data rows instead of raising an error
- Returns `status = -1` to flag the session as a dry run

---

### 4. Temperature bad-value interpolation — extended

Any invalid temperature reading in DTemp1, DTemp2, DTemp3, or ATemp is now
replaced by linear interpolation with neighbouring valid values.

| Condition | v6 | v7 |
|---|---|---|
| `-1000` firmware sentinel | ✗ not handled | ✓ interpolated |
| Any other negative value | ✗ not handled | ✓ interpolated |
| Zero (0.0) | ✗ not handled | ✓ interpolated |
| Empty cell | ✗ parse error | ✓ interpolated |

---

### 5. Kubyk output: 8 → 17 parameters

The processor now populates all parameters already configured in the Kubyk
platform. Previously 9 of these were configured but never receiving data.

| Parameter key | Type | Status |
|---|---|---|
| `raw_input_data` | Non-numeric | unchanged |
| `calibration_input_data` | Non-numeric | unchanged |
| `calibration_output_data` | Non-numeric | unchanged |
| `processed_output_data` | Non-numeric | unchanged |
| `Urine Volume` | Numeric | renamed from `urine_volume` — see note |
| `Urine Sodium` | Numeric | renamed from `urine_sodium` — see note |
| `Urine Potassium` | Numeric | renamed from `urine_potassium` — see note |
| `Na/K Ratio` | Numeric | renamed from `na_k_ratio` — see note |
| `Tact Time` | Numeric | **NEW — now populated** |
| `Urine Temperature` | Numeric | **NEW — now populated** |
| `Environment Temperature` | Numeric | **NEW — now populated** |
| `Urine pH` | Numeric | **NEW — now populated** |
| `Urine Conductivity` | Numeric | **NEW — now populated** |
| `Status` | Numeric | **NEW — now populated** |
| `Patient ID` | Non-numeric* | **NEW — now populated** |
| `Device SN` | Non-numeric | **NEW — now populated** |
| `Calibration Timestamp` | Numeric | **NEW — now populated** |
| `Calibration Completed` | Non-numeric | **NEW — now populated** |

> *`Patient ID` is currently marked as Numeric in Kubyk but now carries an alphanumeric NFC tag value. Needs to update the type to Non-numeric before deploying.

> **Note on renamed parameters:** The 4 primary biomarker keys have been
> updated in the processor code to match the display names in the platform.
> This requires a **coordinated deployment** — Needs to update the
> parameter keys on the Kubyk platform side at the same time.
> Until that coordination happens, the current snake_case keys remain active.

---

## Files included

| File | Description |
|---|---|
| `processor1d.py` | Main service — replace existing file |
| `test_processor1d.py` | Test suite — run before deploying |
| `demo_processor.py` | Local demo with real CSV files |
| `RawData.csv` | Sample V7 raw input data |
| `CalData.csv` | Sample calibration data |
| `requirements.txt` | Python dependencies (unchanged) |
| `README.md` | Full documentation |
| `DEPLOY.md` | Deployment instructions |
| `CHANGELOG.md` | Full version history |

## Verification

Run before deploying:
```bash
pip install numpy
python test_processor1d.py
```

Expected result:
```
Results: 38 passed, 0 failed out of 38 tests
All tests passed. Code is ready for production.
```

`RawData.csv` and `CalData.csv` are included in this package — all 38 tests run fully with no skips.

---

## Open items

| Item | Owner | Notes |
|---|---|---|
| Coordinated deploy of renamed parameter keys | Kubyk/LEDSAS | Must happen simultaneously |
| `raw_input_data` rows | Kubyk | Verify `\r\n` line endings are preserved when displayed in Kubyk |
| `Patient ID` type in Kubyk | Kubyk | Currently marked Numeric in platform — needs to be Non-numeric to support alphanumeric NFC tags |
| Calibration retrieval via API | Future version | - |