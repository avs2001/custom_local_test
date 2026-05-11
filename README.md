# Unephra Processor — Kubyk / LEDSAS Orchestrator
**Version:** 7.0.0

Stateless processing service that receives raw device CSV/JSON data, applies calibration, and returns a 1D biomarker vector to the Kubyk platform via the LEDSAS Orchestrator.

---

## Requirements

- Python 3.9 or higher
- `numpy`
- `kbm_ledsas_sdk` (private — provided by the LEDSAS team)

---

## Installation

```bash
pip install -r requirements.txt
```

---

## Running the service

```bash
python processor1d.py
```

The service registers with the LEDSAS Orchestrator under the name `unephra-processor-direct` and listens for the command `ProcessCSV`.

---

## Running the tests

No SDK required — the test file mocks `kbm_ledsas_sdk` automatically.

```bash
pip install numpy
python test_processor1d.py
```

Expected output:
```
Results: 38 passed, 0 failed out of 38 tests
All tests passed. Code is ready for production.
```

---

## Raw input CSV column structure (V7 — 15 columns)

| # | Field | Type | Notes |
|---|---|---|---|
| 0 | Device SN | numeric | Kubyk device serial number |
| 1 | PatientID | alphanumeric | NFC tag ID; value `"CAL"` = calibration session |
| 2 | SensorID | alphanumeric | Physical UN sensor ID |
| 3 | DateStamp | numeric | Unix epoch seconds |
| 4 | TimeStamp | numeric | ms since session start |
| 5 | DTemp1 | numeric | Device temperature sensor 1 (°C) |
| 6 | DTemp2 | numeric | Device temperature sensor 2 (°C) |
| 7 | DTemp3 | numeric | Device temperature sensor 3 (°C) |
| 8 | ATemp | numeric | Ambient temperature (°C) |
| 9 | VNa | numeric | Sodium sensor voltage (mV) |
| 10 | VK | numeric | Potassium sensor voltage (mV) |
| 11 | VNaK | numeric | NaK sensor voltage (mV) |
| 12 | VpH | numeric | pH sensor voltage (mV) |
| 13 | EC | numeric | Electrical conductivity |
| 14 | Status | numeric | Sensor status (0 = OK) |

**raw_input_data format:** Rows are separated by `\r\n` (line endings) so we can parse the exported data correctly.

**Temperature bad-value handling:** Any negative, zero, or empty cell in DTemp1/2/3 or ATemp columns is replaced by linear interpolation with neighbouring valid values.

---

## Kubyk datasource configuration

| Parameter key | Type | Direction | Description |
|---|---|---|---|
| `raw_input_data` | non-numeric | input (required) | Serialized JSON or CSV of 2D raw device data |
| `calibration_input_data` | non-numeric | input (optional) | Serialized JSON or CSV of calibration 1D vector |
| `calibration_output_data` | non-numeric | output | Calibration vector — only when PatientID == `"CAL"` |
| `processed_output_data` | non-numeric | output | Patient 1D vector — only for patient sessions |
| `Urine Volume` | numeric | output | Urine volume (ml) |
| `Urine Sodium` | numeric | output | Sodium concentration (mEq/L) |
| `Urine Potassium` | numeric | output | Potassium concentration (mEq/L) |
| `Na/K Ratio` | numeric | output | Sodium/potassium ratio |
| `Tact Time` | numeric | output | Active contact duration (s) |
| `Urine Temperature` | numeric | output | Urine temperature (°C) |
| `Environment Temperature` | numeric | output | Environment temperature (°C) |
| `Urine pH` | numeric | output | Urine pH |
| `Urine Conductivity` | numeric | output | Urine electrical conductivity |
| `Status` | numeric | output | Session status (0 = OK, -1 = dry run) |
| `Patient ID` | non-numeric | output | Patient NFC tag ID |
| `Device SN` | non-numeric | output | Device serial number |
| `Calibration Timestamp` | numeric | output | Calibration DateStamp |
| `Calibration Completed` | non-numeric | output | `"true"` / `"false"` |

---

## Patient 1D vector (inside processed_output_data) — 5 elements

```python
[
  "100001,NFC-2749675,SN-001,...,TactTime,UTemp,ETemp,DTemp3,UVolume,...,Status",  # [0] string CSV
  1809.4777,   # [1] urine_volume (ml)
  120.5,       # [2] urine_sodium (mEq/L)
  45.2,        # [3] urine_potassium (mEq/L)
  2.67         # [4] na_k_ratio
]
```

---

## Pending

| Item | Status |
|---|---|
| Calibration retrieval via `GET /api/tenant-admin/parameter-values` | Planned — pending |
| 14-day calibration expiry check | Planned for future version |

---

## Files

```
processor1d.py         — main service
test_processor1d.py    — test suite (38 tests)
demo_processor.py      — local demo with real CSV files
RawData.csv            — sample V7 raw input data
CalData.csv            — sample calibration data
requirements.txt       — Python dependencies
README.md              — this file
DEPLOY.md              — deployment instructions
CHANGELOG.md           — version history
CHANGES_FOR_KUBYK.md   — summary of changes for Kubyk team
```
