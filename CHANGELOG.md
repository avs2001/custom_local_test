# CHANGELOG — Unephra Processor

## v7.0.0

### New fields (raw input CSV expanded from 12 to 15 columns)
- **SensorID** (col 2): alphanumeric physical UN sensor ID, inserted after PatientID
- **DTemp3** (col 7): third device temperature sensor, inserted after DTemp2
- **Status** (col 14): numeric sensor status from firmware (0 = OK), added at end

### Bug fixes
- **PatientID alphanumeric support**: PatientID is now accepted as an alphanumeric NFC tag string (e.g. `"NFC-A3B9"`). Calibration sessions are identified by `PatientID == "CAL"` instead of the previous `PatientID < 0` convention.
- **EC flat / dry run**: When EC signal is flat (all-zero or all-same), the processor no longer raises an error. It falls back to the last 1/3 of rows as the processing window and returns `status = -1` to flag the session as a dry run.


### New features
- **DTemp bad-value interpolation**: Any negative, zero, or empty cell in temperature columns (DTemp1, DTemp2, DTemp3, ATemp) is replaced by linear interpolation with neighbouring valid values. Previously only the `-1000` firmware sentinel was handled.
- **Expanded Kubyk output**: Processor now returns 17 parameters per patient session (previously 8). New parameters use display names matching the Kubyk platform: `Tact Time`, `Urine Temperature`, `Environment Temperature`, `Urine pH`, `Urine Conductivity`, `Status`, `Patient ID`, `Device SN`, `Calibration Timestamp`, `Calibration Completed`.
- **Renamed biomarker parameter keys**: `urine_volume` → `Urine Volume`, `urine_sodium` → `Urine Sodium`, `urine_potassium` → `Urine Potassium`, `na_k_ratio` → `Na/K Ratio` (coordinated deployment with Kubyk platform required).
- **raw_input_data format**: Rows are now separated by `\r\n` (Windows line endings) instead of spaces, so exported data can be opened directly in Excel.
- **Device ID renamed to Device SN**: The device identifier parameter key is now `Device SN` to match the Kubyk platform label.

### Test suite
- Expanded from 19 to 38 tests covering all v7 changes including edge cases, CRLF output, Kubyk parameter keys, and units.

### Pending
- Calibration retrieval via `GET /api/tenant-admin/parameter-values` when no CalData is provided — planned for future version.
- 14-day calibration expiry check — planned for future version.

---

## v6.0.0 — Initial production release

### Features
- V6 spec formulas fully implemented
- Auto-detection of input format: accepts both CSV string and JSON serialized matrix
- Identity calibration fallback when CalData is not available (returns raw voltages with WARNING)
- Patient session: returns `processed_output_data` (1D vector, serialized JSON) + 4 discrete params
- Calibration session: returns `calibration_output_data` as JSON when PatientID < 0
- Backward compatible field names: accepts `raw_input_data` and legacy `RawData`

### Output structure
- `raw_input_data` — non-numeric, always returned
- `calibration_input_data` — non-numeric, always returned
- `calibration_output_data` — non-numeric, only for calibration sessions (PatientID < 0)
- `processed_output_data` — non-numeric, serialized JSON 1D vector, only for patient sessions
- `urine_volume` — numeric (ml)
- `urine_sodium` — numeric (mEq/L or mV if no CalData)
- `urine_potassium` — numeric (mEq/L or mV if no CalData)
- `na_k_ratio` — numeric

### Validations
- `deviceSerialNumber` must match `DeviceID` in RawData numerically
- PatientID = 0 rejected; PatientID < 0 handled as calibration session
- Trailing empty columns and Excel artifacts (`#REF!`, `#N/A`, `#VALUE!`) stripped
- Input size limit: 5 MB
- All numeric values validated as finite floats

### Pending
- Real CalData for device 10201: example CalData provided with clinical example coefficients
- 14-day calibration expiry check planned for future version
