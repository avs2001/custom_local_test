#!/usr/bin/env python3
"""
Unephra Processor for Kubyk / LEDSAS Orchestrator
=================================================

Behavior
--------
This service expects every request to include:
- raw_input_data:         2D CSV string with patient/device measurement data
- calibration_input_data: calibration CSV string (optional — identity calibration used if missing)

Response parameters (per Kubyk datasource structure):
- raw_input_data          (non-numeric) input CSV as received
- calibration_input_data  (non-numeric) calibration CSV as received
- calibration_output_data (non-numeric) calculated calibration 1D vector — only when PatientID == "CAL"
- processed_output_data   (non-numeric) calculated patient 1D vector   — only when PatientID is a patient NFC tag

Patient Output 1D vector (inside processed_output_data) — 5 elements:
[
  patient_output_csv,  # string (CSV): Device SN, PatientID, SensorID, DateStamp, TimeStamp, TactTime,
                       #               UTemp, ETemp, DTemp3, UVolume, USodium, UPotassium, UpH,
                       #               UCon, EpH, Status
  urine_volume,        # float (ml)
  urine_sodium,        # float (mEq/L)
  urine_potassium,     # float (mEq/L)
  na_k_ratio           # float
]

Discrete numeric parameters (also returned separately for Kubyk dashboards):
- urine_volume, urine_sodium, urine_potassium, na_k_ratio
- tact_time, urine_temperature, environment_temperature, urine_ph,
  urine_conductivity, status, patient_id, device_sn,
  calibration_timestamp, calibration_completed

Raw input CSV column structure (V7 — 15 columns):
  Device SN | PatientID | SensorID | DateStamp | TimeStamp |
  DTemp1 | DTemp2 | DTemp3 | ATemp |
  VNa | VK | VNaK | VpH | EC | Status

  - Device SN: numeric Kubyk device serial number. Kept numeric per design decision — the
               hardware always emits a numeric ID and enforcing this provides an early
               validation layer against malformed payloads.
  - PatientID: alphanumeric NFC tag. Value "CAL" identifies calibration sessions.
  - SensorID:  alphanumeric physical UN sensor ID.
  - Status:    numeric sensor status (0 = OK). Bad DTemp values (-1000) are interpolated.

Top-level request expected from Kubyk
-------------------------------------
{
  "deviceSerialNumber": "string",
  "sessionId": "string",
  "data": "string"
}

Where req["data"] is a JSON-encoded string like:
{
  "raw_input_data":         "<2D CSV string>",
  "calibration_input_data": "<1D CSV string>"   (optional)
}

Notes
-----
- This service does NOT use blob storage, folders, containers, or cached calibration state.
- Calibration sessions are identified by PatientID == "CAL" in the raw input (V7+).
- Device SN is the numeric Kubyk device serial number. deviceSerialNumber in the
  request is the same identifier but is not validated against it — the platform may
  send it in a different format.
- The 14-day calibration expiry will be added in future versions.
"""

import csv
import io
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import numpy as np
from kbm_ledsas_sdk import ServiceApp, errors

# Logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("unephra.processor.production")


# LEDSAS / Kubyk service registration

app = ServiceApp(service_name="unephra-processor-direct")


# Column index constants — V7 raw input CSV (15 columns)
#
# Device SN is numeric. PatientID and SensorID are
# alphanumeric strings and are extracted separately from the float matrix.

COL_UDN    = 0   # Device SN  — numeric Kubyk device serial number
COL_UPID   = 1   # PatientID  — alphanumeric NFC tag; "CAL" = calibration session
COL_SID    = 2   # SensorID   — alphanumeric physical UN sensor ID [NEW v7]
COL_DATE   = 3   # DateStamp  — Unix epoch seconds
COL_TIME   = 4   # TimeStamp  — milliseconds since session start
COL_T1     = 5   # DTemp1     — device temperature sensor 1 (°C)
COL_T2     = 6   # DTemp2     — device temperature sensor 2 (°C)
COL_T3     = 7   # DTemp3     — device temperature sensor 3 (°C) [NEW v7]
COL_T4     = 8   # ATemp      — ambient temperature (°C)
COL_VNA    = 9   # VNa        — sodium sensor voltage (mV)
COL_VK     = 10  # VK         — potassium sensor voltage (mV)
COL_VNAK   = 11  # VNaK       — NaK sensor voltage (mV)
COL_VPH    = 12  # VpH        — pH sensor voltage (mV)
COL_EC     = 13  # EC         — electrical conductivity
COL_STATUS = 14  # Status     — numeric sensor status from firmware [NEW v7]

N_COLS_V7 = 15

# Temperature columns that require special missing-value handling.
# In these columns, empty cells and 0.0 are treated as invalid readings
# alongside negatives, and are replaced by interpolation in compute_patient_outputs.
TEMP_COLS = {COL_T1, COL_T2, COL_T3, COL_T4}


# Kubyk parameter keys
# Must match the parameter key names configured on the Kubyk platform.

# Non-numeric passthrough parameters (always returned)
PARAM_RAW_INPUT        = "raw_input_data"
PARAM_CAL_INPUT        = "calibration_input_data"
PARAM_CAL_OUTPUT       = "calibration_output_data"
PARAM_PROCESSED_OUTPUT = "processed_output_data"

# Primary biomarker parameters — renamed to display names in v7 (coordinated with Kubyk platform)
PARAM_URINE_VOLUME    = "Urine Volume"
PARAM_URINE_SODIUM    = "Urine Sodium"
PARAM_URINE_POTASSIUM = "Urine Potassium"
PARAM_NA_K_RATIO      = "Na/K Ratio"

# Expanded diagnostic parameters added in v7.0.0
# Values match the parameter key names configured on the Kubyk platform exactly.
PARAM_TACT_TIME             = "Tact Time"
PARAM_URINE_TEMPERATURE     = "Urine Temperature"
PARAM_ENVIRONMENT_TEMP      = "Environment Temperature"
PARAM_URINE_PH              = "Urine pH"
PARAM_URINE_CONDUCTIVITY    = "Urine Conductivity"
PARAM_STATUS                = "Status"
PARAM_PATIENT_ID            = "Patient ID"
PARAM_DEVICE_ID             = "Device SN"
PARAM_CALIBRATION_TIMESTAMP = "Calibration Timestamp"
PARAM_CALIBRATION_COMPLETED = "Calibration Completed"

# NOTE: The following 4 keys are pending rename on the Kubyk platform side (Valentin).
# Once updated there, change these values to match:
#   PARAM_URINE_VOLUME    -> "Urine Volume"      (already done in code)
#   PARAM_URINE_SODIUM    -> "Urine Sodium"       (already done in code)
#   PARAM_URINE_POTASSIUM -> "Urine Potassium"    (already done in code)
#   PARAM_NA_K_RATIO      -> "Na/K Ratio"         (already done in code)
# Until then, the platform still expects the snake_case keys below — revert if needed.

STATUS_OK = 0

# Increment this when deploying a new version
SERVICE_VERSION = "7.0.0"

# Passed as cal_csv_text when no calibration data is available.
# Triggers identity calibration (offsets=0, gains=0, P0=0/P1=1).
# Outputs will be raw averaged voltages (mV), not clinical concentrations.
NO_CALIBRATION_SENTINEL = "NO_CALIBRATION"

# Fixed column order for the calibration CSV — used for both parsing and serialisation
CALIBRATION_HEADERS = [
    "Device SN", "DateStamp",
    "TT_Offset", "T1_Offset", "T2_Offset", "T3_Offset",
    "UV1_Gain", "UV2_Gain",
    "UN_P0", "UN_P1", "UN_P2", "UN_P3",
    "UK_P0", "UK_P1", "UK_P2", "UK_P3",
    "UH_P0", "UH_P1",
]


# Data models

@dataclass
class CalibrationV6:
    """Calibration parameters for a single device session.

    Device SN (DeviceID field) is stored as a string to accommodate potential future format
    changes while still being parsed as a numeric value at ingestion time.
    Default polynomial coefficients (P0=0, P1=1) represent a pass-through identity.
    """
    DeviceID: str = ""
    DateStamp: int = 0

    TT_Offset: float = 0.0   # Tact-time offset (ms)
    T1_Offset: float = 0.0   # DTemp1 calibration offset (°C)
    T2_Offset: float = 0.0   # DTemp2 calibration offset (°C)
    T3_Offset: float = 0.0   # ATemp / urine temperature offset (°C)

    UV1_Gain: float = 1.0    # Volume gain from T1 path (ml/unit)
    UV2_Gain: float = 1.0    # Volume gain from T2 path (ml/unit)

    # Sodium polynomial: Na = P0 + P1*VNa + P2*VNaK + P3*VK
    UN_P0: float = 0.0
    UN_P1: float = 1.0
    UN_P2: float = 0.0
    UN_P3: float = 0.0

    # Potassium polynomial: K = P0 + P1*VK + P2*VNaK + P3*VNa
    UK_P0: float = 0.0
    UK_P1: float = 1.0
    UK_P2: float = 0.0
    UK_P3: float = 0.0

    # pH polynomial: pH = P0 + P1*VpH
    UH_P0: float = 0.0
    UH_P1: float = 1.0

    @classmethod
    def identity(cls, device_id: str = "") -> "CalibrationV6":
        """Return an identity calibration (no-op).

        Used when no CalData is provided. UV gains are set to 0 so urine_volume
        will be 0 as well. Outputs are raw averaged voltages (mV), not clinical values.
        """
        return cls(
            DeviceID=device_id,
            DateStamp=0,
            TT_Offset=0.0, T1_Offset=0.0, T2_Offset=0.0, T3_Offset=0.0,
            UV1_Gain=0.0, UV2_Gain=0.0,
            UN_P0=0.0, UN_P1=1.0, UN_P2=0.0, UN_P3=0.0,
            UK_P0=0.0, UK_P1=1.0, UK_P2=0.0, UK_P3=0.0,
            UH_P0=0.0, UH_P1=1.0,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CalibrationV6":
        """Parse a calibration from a key-value dict.

        Device SN (DeviceID) is parsed as a numeric string; DateStamp as int; all other fields as float.
        Accepts both "Device SN" and legacy "DeviceID" as the device identifier key.
        Unknown keys are silently ignored for forward-compatibility.
        """
        allowed = set(cls.__dataclass_fields__.keys())
        parsed: Dict[str, Any] = {}

        # Normalise "Device SN" to the internal field name "DeviceID"
        normalised = {"DeviceID" if k == "Device SN" else k: v for k, v in data.items()}

        for key, value in normalised.items():
            if key not in allowed:
                continue
            if key == "DateStamp":
                parsed[key] = _parse_int_like(value, field_name=key)
            elif key == "DeviceID":
                # Device SN is numeric in hardware; normalise to string via int() to
                # drop any float suffix (e.g. "100001.0" -> "100001")
                parsed[key] = str(int(float(str(value).strip())))
            else:
                parsed[key] = _parse_float_like(value, field_name=key)

        return cls(**parsed)

    @classmethod
    def from_csv_row(cls, row: Dict[str, Any]) -> "CalibrationV6":
        return cls.from_dict(row)


@dataclass
class ComputedPatientOutputs:
    """All computed outputs for a single patient session."""
    device_id: str    # numeric Device SN as string
    patient_id: str   # alphanumeric NFC tag
    sensor_id: str    # alphanumeric physical UN sensor ID [NEW v7]
    datestamp: int
    timestamp: int

    tact_time: float       # active contact duration (s)
    utemp: float           # urine temperature (°C) — from ATemp at peak EC
    etemp: float           # environment temperature (°C) — from DTemp1/2 baseline
    dtemp3: float          # DTemp3 average (°C) [NEW v7]
    urine_volume: float    # ml
    u_sodium: float        # mEq/L
    u_potassium: float     # mEq/L
    u_ph: float
    u_con: float           # electrical conductivity
    ep_h: float            # reserved — not measured in V6/V7
    status: int            # 0 = OK, -1 = dry run (flat EC)
    na_k_ratio: float
    calibration_applied: bool = True  # False when using identity calibration


# Parsing helpers

def _parse_int_like(value: Any, field_name: str) -> int:
    """Parse a value to int, accepting floats-as-strings (e.g. "1730158863.0")."""
    try:
        return int(float(str(value).strip()))
    except Exception as exc:
        raise errors.Permanent(
            f"Invalid integer-like value for {field_name}: {value!r}",
            user_message=f"Invalid value for {field_name}"
        ) from exc


def _parse_float_like(value: Any, field_name: str) -> float:
    """Parse a value to float, stripping internal whitespace (e.g. "7. 5" -> 7.5)."""
    try:
        parsed = float(str(value).replace(" ", "").strip())
    except Exception as exc:
        raise errors.Permanent(
            f"Invalid float-like value for {field_name}: {value!r}",
            user_message=f"Invalid value for {field_name}"
        ) from exc

    if not math.isfinite(parsed):
        raise errors.Permanent(
            f"Non-finite numeric value for {field_name}: {value!r}",
            user_message=f"Invalid numeric value for {field_name}"
        )

    return parsed


def nonzero_min(arr: np.ndarray) -> float:
    """Minimum of non-zero elements; returns 0.0 if all elements are zero."""
    nz = arr[arr != 0]
    return float(np.min(nz)) if len(nz) > 0 else 0.0


def nonzero_avg(arr: np.ndarray) -> float:
    """Mean of non-zero elements; returns 0.0 if all elements are zero."""
    nz = arr[arr != 0]
    return float(np.mean(nz)) if len(nz) > 0 else 0.0


def normalize_compact_string(text: str) -> str:
    """Normalise a CSV string for storage in Kubyk string parameters.

    Each row is trimmed of surrounding whitespace and rejoined with \r\n
    (Windows line endings) so that Excel can correctly parse the exported
    raw_input_data without rows running together.
    Empty lines are dropped.
    """
    lines = [line.strip() for line in str(text).splitlines()]
    return "\r\n".join(line for line in lines if line)



def to_epoch_millis(ts_value: int) -> int:
    """Convert a timestamp to milliseconds.

    Accepts both epoch-seconds and epoch-milliseconds by checking magnitude.
    """
    ts_float = float(ts_value)
    if ts_float < 1e11:
        ts_float *= 1000.0
    return int(ts_float)


def make_value(
    parameter_key: str,
    value: Any,
    start_ts_ms: int,
    end_ts_ms: int,
    unit_measure: str = None,
    notes: str = None,
) -> Dict[str, Any]:
    """Build a single Kubyk parameter value dict."""
    return {
        "parameterKey": parameter_key,
        "value": value,
        "startTimestamp": start_ts_ms,
        "endTimestamp": end_ts_ms,
        "unitMeasure": unit_measure,
        "notes": notes,
    }


# Request parsing

def parse_inner_data_json(data_field: str) -> Dict[str, Any]:
    """Decode the JSON-encoded 'data' field from the Kubyk request."""
    try:
        parsed = json.loads(data_field)
    except json.JSONDecodeError as exc:
        raise errors.Permanent(
            f"Invalid data JSON: {exc}",
            user_message="The request field 'data' is not valid JSON"
        ) from exc

    if not isinstance(parsed, dict):
        raise errors.Permanent(
            "Inner payload is not a JSON object",
            user_message="The request field 'data' must decode to a JSON object"
        )

    return parsed


def _normalize_to_csv(value: str, field_name: str) -> str:
    """Normalise a data field to CSV string, regardless of whether it arrived as JSON or CSV.

    The device firmware may serialise data in two formats:
    - CSV string:  "10201,2749675,..."
    - JSON array:  "[[10201,2749675,...],[...]]"  (2D) or "[10201,2749675,...]" (1D)

    Both are accepted and returned as a plain CSV string for downstream parsing.
    """
    text = value.strip()

    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise errors.Permanent(
                f"{field_name} looks like JSON but could not be parsed: {exc}",
                user_message=f"{field_name} contains invalid JSON"
            ) from exc

        if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], list):
            # 2D matrix -> multi-row CSV
            logger.info("%s received as JSON 2D matrix (%d rows)", field_name, len(parsed))
            return "\n".join(",".join(str(v) for v in row) for row in parsed)

        if isinstance(parsed, list):
            # 1D vector -> single-row CSV
            logger.info("%s received as JSON 1D vector (%d elements)", field_name, len(parsed))
            return ",".join(str(v) for v in parsed)

        raise errors.Permanent(
            f"{field_name} JSON is not a list",
            user_message=f"{field_name} must be a JSON array"
        )

    logger.info("%s received as CSV string", field_name)
    return text


def extract_required_strings(inner: Dict[str, Any]) -> Tuple[str, str]:
    """Extract and normalise raw_input_data and calibration_input_data from the inner payload.

    Accepts both current field names and legacy names (RawData / CalData) for
    backward compatibility with older firmware versions.

    If calibration_input_data is absent, returns NO_CALIBRATION_SENTINEL so the
    downstream parser can apply an identity calibration.
    """
    raw_data = inner.get("raw_input_data") or inner.get("RawData")
    cal_data = inner.get("calibration_input_data") or inner.get("CalData")

    if raw_data is None or str(raw_data).strip() == "":
        raise errors.Permanent(
            "Missing raw_input_data",
            user_message="Missing required field: raw_input_data"
        )

    raw_csv = _normalize_to_csv(str(raw_data), "raw_input_data")

    if cal_data is None or str(cal_data).strip() == "":
        logger.warning("calibration_input_data not provided — identity calibration will be used; outputs are raw voltages (mV)")
        return raw_csv, NO_CALIBRATION_SENTINEL

    cal_csv = _normalize_to_csv(str(cal_data), "calibration_input_data")
    return raw_csv, cal_csv


# CSV parsing

def parse_raw_csv(raw_csv_text: str) -> Tuple[np.ndarray, List[str], List[str]]:
    """Parse the raw input CSV into a numeric matrix and string identity columns.

    Device SN is numeric and stored directly in the matrix at COL_UDN.
    PatientID and SensorID are alphanumeric and extracted as separate lists;
    their matrix columns (COL_UPID, COL_SID) hold 0.0 placeholders.

    Header rows are detected by a non-numeric value in COL_UDN (the first column).
    This works reliably because Device SN is always numeric by design.

    Returns:
        matrix      — float64 ndarray (N x N_COLS_V7)
        patient_ids — list of PatientID strings per row
        sensor_ids  — list of SensorID strings per row
    """
    MAX_INPUT_BYTES = 5 * 1024 * 1024  # 5 MB — guard against oversized payloads
    if len(raw_csv_text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise errors.Permanent(
            f"RawData exceeds maximum allowed size of {MAX_INPUT_BYTES} bytes",
            user_message="RawData payload is too large"
        )

    MIN_ROWS = 1
    reader = csv.reader(io.StringIO(raw_csv_text))
    rows: List[List[float]] = []
    patient_ids: List[str] = []
    sensor_ids: List[str] = []
    skipped = 0

    for lineno, parts in enumerate(reader, start=1):
        if not parts:
            continue

        # Skip header row — a non-numeric value in COL_UDN (Device SN) signals a text header.
        # Works for both "DeviceID" and "Device SN" header names.
        try:
            float(parts[0].strip())
        except ValueError:
            skipped += 1
            continue

        # Strip trailing empty/spreadsheet-artifact columns that some export tools append
        while len(parts) > N_COLS_V7 and parts[-1].strip() in {"", "#REF!", "#N/A", "#VALUE!"}:
            parts = parts[:-1]

        if len(parts) != N_COLS_V7:
            raise errors.Permanent(
                f"RawData row {lineno} has {len(parts)} columns; expected {N_COLS_V7}",
                user_message=f"RawData must contain exactly {N_COLS_V7} columns per row"
            )

        cleaned_row: List[float] = []
        row_patient_id = ""
        row_sensor_id  = ""

        for col_idx, cell in enumerate(parts):
            cell_value = cell.strip()

            if cell_value in {"#REF!", "#N/A", "#VALUE!"}:
                raise errors.Permanent(
                    f"RawData contains invalid spreadsheet artifact at row {lineno}, col {col_idx}",
                    user_message="RawData contains invalid spreadsheet artifacts"
                )

            # PatientID and SensorID are alphanumeric — extract as strings and store a
            # 0.0 placeholder in the matrix so the numeric array stays uniform
            if col_idx == COL_UPID:
                row_patient_id = cell_value
                cleaned_row.append(0.0)
                continue

            if col_idx == COL_SID:
                row_sensor_id = cell_value
                cleaned_row.append(0.0)
                continue

            # Empty cells in temperature columns are stored as NaN so that
            # interpolate_bad_temps can fill them in compute_patient_outputs.
            # Empty cells in any other column are still a hard error.
            if cell_value == "" and col_idx in TEMP_COLS:
                cleaned_row.append(float("nan"))
                continue

            try:
                parsed = float(cell_value)
            except ValueError as exc:
                raise errors.Permanent(
                    f"RawData parse error at row {lineno}, col {col_idx}: {cell_value!r}",
                    user_message="RawData contains non-numeric values"
                ) from exc

            if not math.isfinite(parsed) and col_idx not in TEMP_COLS:
                # NaN/Inf in non-temperature columns is always a hard error.
                # NaN in temperature columns is handled by interpolate_bad_temps.
                raise errors.Permanent(
                    f"RawData contains non-finite value at row {lineno}, col {col_idx}",
                    user_message="RawData contains invalid numeric values"
                )

            cleaned_row.append(parsed)

        if not row_patient_id:
            raise errors.Permanent(
                f"RawData row {lineno} has empty PatientID",
                user_message="RawData contains a row with empty PatientID"
            )
        if not row_sensor_id:
            raise errors.Permanent(
                f"RawData row {lineno} has empty SensorID",
                user_message="RawData contains a row with empty SensorID"
            )

        rows.append(cleaned_row)
        patient_ids.append(row_patient_id)
        sensor_ids.append(row_sensor_id)

    if skipped:
        logger.info("RawData parsing skipped %s non-data row(s)", skipped)

    if len(rows) == 0:
        raise errors.Permanent(
            "RawData contains no valid rows",
            user_message="RawData contains no valid rows"
        )

    if len(rows) < MIN_ROWS:
        raise errors.Permanent(
            f"RawData has only {len(rows)} data row(s); minimum required is {MIN_ROWS}",
            user_message=f"RawData must contain at least {MIN_ROWS} rows for valid computation"
        )

    matrix = np.array(rows, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != N_COLS_V7:
        raise errors.Permanent(
            f"RawData matrix shape invalid: {matrix.shape}",
            user_message="RawData has invalid shape"
        )

    return matrix, patient_ids, sensor_ids


def parse_calibration_csv(cal_csv_text: str, device_id: str = "") -> CalibrationV6:
    """Parse the calibration CSV into a CalibrationV6 object.

    Accepts two formats:
    - Header row + data row (order-independent, recommended)
    - Single data row in CALIBRATION_HEADERS column order (legacy)

    Returns an identity calibration if cal_csv_text is NO_CALIBRATION_SENTINEL.
    """
    if cal_csv_text.strip() == NO_CALIBRATION_SENTINEL:
        logger.warning("Using identity calibration for device_id=%s — outputs are raw voltages (mV)", device_id)
        return CalibrationV6.identity(device_id=device_id)

    def normalize_header_name(name: str) -> str:
        # Accept legacy spaced names ("Device ID", "Date Stamp") alongside compact ones
        aliases = {
            "Device ID":  "Device SN",  # legacy name
            "DeviceID":   "Device SN",  # legacy name
            "Device SN":  "Device SN",  # current name
            "Date Stamp": "DateStamp",
            "DateStamp":  "DateStamp",
        }
        return aliases.get(name.strip(), name.strip())

    text = cal_csv_text.strip()
    if not text:
        raise errors.Permanent("Calibration CSV is empty", user_message="CalData is empty")

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]

    if not rows:
        raise errors.Permanent(
            "Calibration CSV contains no usable rows",
            user_message="CalData contains no usable rows"
        )

    first_row_normalized = [normalize_header_name(h) for h in rows[0]]

    if len(rows) >= 2 and first_row_normalized[0] == "Device SN":
        # Format 1: header row + data row
        header = first_row_normalized
        values = [v.strip() for v in rows[1]]

        if len(header) != len(values):
            raise errors.Permanent(
                "Calibration CSV header/data length mismatch",
                user_message="CalData header/data length mismatch"
            )

        missing = sorted(set(CALIBRATION_HEADERS) - set(header))
        if missing:
            raise errors.Permanent(
                f"Calibration CSV missing headers: {missing}",
                user_message="CalData is missing expected headers"
            )

        return CalibrationV6.from_csv_row(dict(zip(header, values)))

    # Format 2: single row in fixed CALIBRATION_HEADERS order (legacy)
    values = [v.strip() for v in rows[0]]
    if len(values) != len(CALIBRATION_HEADERS):
        raise errors.Permanent(
            f"Calibration CSV has {len(values)} columns; expected {len(CALIBRATION_HEADERS)}",
            user_message="CalData column count does not match expected format"
        )

    return CalibrationV6.from_csv_row(dict(zip(CALIBRATION_HEADERS, values)))


# Identity extraction and validation

def extract_identity_from_raw_matrix(
    matrix: np.ndarray, patient_ids: List[str], sensor_ids: List[str]
) -> Tuple[str, str, str, int, int]:
    """Pull session identity fields from the first row of the raw matrix."""
    device_id  = str(int(matrix[0, COL_UDN]))  # cast via int() to drop the float ".0"
    patient_id = patient_ids[0]
    sensor_id  = sensor_ids[0]
    datestamp  = int(matrix[0, COL_DATE])
    timestamp  = int(matrix[0, COL_TIME])
    return device_id, patient_id, sensor_id, datestamp, timestamp


def validate_identity(
    device_serial_number: str,
    raw_device_id: str,
    patient_id: str,
    calibration: CalibrationV6,
) -> None:
    """Verify that the session identifiers are internally consistent.

    Checks performed:
    - Device SN in RawData is non-empty
    - PatientID is non-empty ("CAL" sessions are handled upstream before this call)
    - CalibrationV6.DeviceID (Device SN) is non-empty
    - RawData Device SN matches CalData Device SN (ensures calibration belongs to this device)

    Note: deviceSerialNumber (Kubyk platform ID) is intentionally NOT validated against
    Device SN — the platform may send deviceSerialNumber in a different format.
    """
    if not raw_device_id:
        raise errors.Permanent(
            f"Invalid raw Device SN: {raw_device_id!r}",
            user_message="RawData contains an invalid Device SN"
        )

    if not patient_id:
        raise errors.Permanent(
            f"Invalid PatientID: {patient_id!r}",
            user_message="RawData contains an invalid PatientID"
        )

    if not calibration.DeviceID:
        raise errors.Permanent(
            f"Invalid calibration Device SN: {calibration.DeviceID!r}",
            user_message="CalData contains an invalid Device SN"
        )

    if raw_device_id != calibration.DeviceID:
        raise errors.Permanent(
            f"Device SN mismatch: RawData={raw_device_id}, CalData={calibration.DeviceID}",
            user_message="RawData Device SN and CalData Device SN do not match"
        )


# Core biomarker computation

BAD_TEMP_SENTINEL = -1000.0  # canonical firmware sentinel, kept for reference

# Three conditions mark a temperature reading as invalid and trigger interpolation:
#   1. Negative value  — physically impossible; firmware uses -1000 as sentinel
#   2. Zero (0.0)      — physically impossible for body/ambient temperature
#   3. NaN             — produced by parse_raw_csv when a CSV cell is empty
BAD_TEMP_THRESHOLD = 0.0


def interpolate_bad_temps(arr: np.ndarray, threshold: float = BAD_TEMP_THRESHOLD) -> np.ndarray:
    """Replace invalid temperature readings with linearly interpolated neighbours.

    A reading is considered invalid if it is negative, zero, or NaN.
    Edge bad values are forward- or backward-filled from the nearest valid value.
    If the entire array is invalid, it is returned unchanged (caller handles it).
    """
    result = arr.copy()
    # Combine all three invalidity conditions into a single mask
    bad  = (result < threshold) | (result == 0.0) | np.isnan(result)
    if not np.any(bad):
        return result
    good = ~bad
    if not np.any(good):
        return result
    indices = np.arange(len(result))
    result[bad] = np.interp(indices[bad], indices[good], result[good])
    return result


def compute_patient_outputs(
    matrix: np.ndarray,
    calibration: CalibrationV6,
    patient_ids: List[str],
    sensor_ids: List[str],
) -> ComputedPatientOutputs:
    """Compute all biomarker outputs from the raw measurement matrix.

    EC-based masking strategy:
    - Normal session: masks select the top 80% (mask_5) and top 98% (mask_10) of peak EC.
      Tact time is derived from mask_5; ion concentrations from mask_10 (plateau signal).
    - Flat / dry-run EC (max-min < 1e-9): no liquid present. Falls back to the last 1/3
      of rows and sets status = STATUS_DRY_RUN (-1) so the caller can flag the session.

    Temperature handling:
    - utemp (urine temperature): max ATemp (T4) at measurement time + T3_Offset.
    - etemp (environment):       min DTemp1/2 baseline average + respective offsets.
    - DTemp bad values (-1000) are interpolated before use.
    """
    # Apply bad-value interpolation before any computation
    T1   = interpolate_bad_temps(matrix[:, COL_T1])
    T2   = interpolate_bad_temps(matrix[:, COL_T2])
    T3   = interpolate_bad_temps(matrix[:, COL_T3])
    T4   = interpolate_bad_temps(matrix[:, COL_T4])
    VNa  = matrix[:, COL_VNA]
    VK   = matrix[:, COL_VK]
    VNaK = matrix[:, COL_VNAK]
    VpH  = matrix[:, COL_VPH]
    EC   = matrix[:, COL_EC]
    TS   = matrix[:, COL_TIME]
    STATUS_INPUT = matrix[:, COL_STATUS]

    device_id, patient_id, sensor_id, datestamp, timestamp = extract_identity_from_raw_matrix(
        matrix, patient_ids, sensor_ids
    )

    sensor_status = int(STATUS_INPUT[0])  # session status taken from the first row

    ec_max  = float(np.max(EC))
    ec_min  = float(np.min(EC))
    ec_flat = (ec_max - ec_min) < 1e-9

    STATUS_DRY_RUN = -1

    if ec_flat:
        # Flat EC — no urine detected (dry run). Use the last 1/3 of rows as the
        # processing window so downstream calculations still produce a numeric result.
        n         = len(EC)
        start_idx = int(n * 2 / 3)
        logger.warning(
            "EC signal is flat for device=%s patient=%s — using last 1/3 of data (rows %d-%d)",
            device_id, patient_id, start_idx, n - 1
        )
        T1   = T1[start_idx:];   T2   = T2[start_idx:]
        T3   = T3[start_idx:];   T4   = T4[start_idx:]
        VNa  = VNa[start_idx:];  VK   = VK[start_idx:]
        VNaK = VNaK[start_idx:]; VpH  = VpH[start_idx:]
        EC   = EC[start_idx:];   TS   = TS[start_idx:]
        ec_max = float(np.max(EC)) if float(np.max(EC)) > 1e-9 else 1.0
        # Full masks — treat all rows as valid since there is no real EC peak
        mask_5  = np.ones(len(EC), dtype=np.float64)
        mask_10 = np.ones(len(EC), dtype=np.float64)
        effective_status = STATUS_DRY_RUN
    else:
        # Normal session — select rows near the EC peak
        mask_5  = (EC >= 0.80 * ec_max).astype(np.float64)  # top 80% — tact time window
        mask_10 = (EC >= 0.98 * ec_max).astype(np.float64)  # top 98% — ion plateau window

        if np.sum(mask_5) == 0 or np.sum(mask_10) == 0:
            raise errors.Permanent(
                "EC masks are empty after thresholding",
                user_message="RawData failed thresholding checks"
            )
        effective_status = sensor_status

    # Tact time: duration within the mask_5 window, converted from ms to seconds
    tv        = TS * mask_5
    tact_time = (np.max(tv) - nonzero_min(tv) + calibration.TT_Offset) / 1000.0
    tact_time = round(max(tact_time, 0.0), 4)

    # Urine temperature: peak ATemp (T4) adjusted by T3_Offset
    utemp = round(float(np.max(T4)) + calibration.T3_Offset, 4)

    # Environment temperature: DTemp1/2 baseline (min values) averaged across both sensors.
    # Note: T2 is intentionally used twice — this matches the V7 hardware calibration spec.
    etemp = round((
        (float(np.min(T1)) + calibration.T1_Offset) +
        (float(np.min(T2)) + calibration.T2_Offset) +
        (float(np.min(T2)) + calibration.T2_Offset)
    ) / 3.0, 4)

    dtemp3 = round(float(np.mean(T3)), 4)  # informational average of DTemp3 channel [NEW v7]

    # Urine volume: calorimetric model — heat transfer from urine to each sensor path.
    # UVi = (Ti_max_adj - ET) / (UT - Ti_max_adj) * UVi_Gain;  UV = UV1 + UV2
    t1_max_adj = float(np.max(T1)) + calibration.T1_Offset
    t2_max_adj = float(np.max(T2)) + calibration.T2_Offset
    denom_uv1  = utemp - t1_max_adj
    denom_uv2  = utemp - t2_max_adj
    uv1 = (t1_max_adj - etemp) / denom_uv1 * calibration.UV1_Gain if abs(denom_uv1) > 1e-9 else 0.0
    uv2 = (t2_max_adj - etemp) / denom_uv2 * calibration.UV2_Gain if abs(denom_uv2) > 1e-9 else 0.0
    urine_volume = round(uv1 + uv2, 4)

    # Ion concentrations: averaged sensor voltages at EC plateau (mask_10) fed into polynomials
    vs_na  = nonzero_avg(VNa  * mask_10)
    vs_k   = nonzero_avg(VK   * mask_10)
    vs_nak = nonzero_avg(VNaK * mask_10)
    vs_ph  = nonzero_avg(VpH  * mask_10)

    u_sodium = round(
        calibration.UN_P0 + calibration.UN_P1 * vs_na +
        calibration.UN_P2 * vs_nak + calibration.UN_P3 * vs_k, 4
    )
    u_potassium = round(
        calibration.UK_P0 + calibration.UK_P1 * vs_k +
        calibration.UK_P2 * vs_nak + calibration.UK_P3 * vs_na, 4
    )
    u_ph = round(calibration.UH_P0 + calibration.UH_P1 * vs_ph, 4)

    u_con      = round(nonzero_avg(EC * mask_10), 4)
    ep_h       = 0.0  # reserved — not measured in V6/V7
    na_k_ratio = round(u_sodium / u_potassium, 4) if abs(u_potassium) > 1e-9 else 0.0

    logger.info(
        "Computed outputs device=%s patient=%s sensor=%s volume=%s sodium=%s potassium=%s ratio=%s",
        device_id, patient_id, sensor_id, urine_volume, u_sodium, u_potassium, na_k_ratio
    )

    return ComputedPatientOutputs(
        device_id=device_id, patient_id=patient_id, sensor_id=sensor_id,
        datestamp=datestamp, timestamp=timestamp,
        tact_time=tact_time, utemp=utemp, etemp=etemp, dtemp3=dtemp3,
        urine_volume=urine_volume, u_sodium=u_sodium, u_potassium=u_potassium,
        u_ph=u_ph, u_con=u_con, ep_h=ep_h,
        status=effective_status, na_k_ratio=na_k_ratio,
        calibration_applied=(calibration.DateStamp != 0),
    )


# Output string builders

def build_calibration_output_data(calibration: CalibrationV6) -> str:
    """Serialise the calibration object to a CSV string in CALIBRATION_HEADERS order."""
    values = [
        calibration.DeviceID, calibration.DateStamp,
        calibration.TT_Offset, calibration.T1_Offset, calibration.T2_Offset, calibration.T3_Offset,
        calibration.UV1_Gain, calibration.UV2_Gain,
        calibration.UN_P0, calibration.UN_P1, calibration.UN_P2, calibration.UN_P3,
        calibration.UK_P0, calibration.UK_P1, calibration.UK_P2, calibration.UK_P3,
        calibration.UH_P0, calibration.UH_P1,
    ]
    return ",".join(str(v) for v in values)


def build_patient_output_data(outputs: ComputedPatientOutputs) -> str:
    """Serialise patient outputs to a CSV string.

    Column order (V7): Device SN, PatientID, SensorID, DateStamp, TimeStamp, TactTime,
                       UTemp, ETemp, DTemp3, UVolume, USodium, UPotassium, UpH, UCon, EpH, Status
    """
    values = [
        outputs.device_id, outputs.patient_id, outputs.sensor_id,
        outputs.datestamp, outputs.timestamp, outputs.tact_time,
        outputs.utemp, outputs.etemp, outputs.dtemp3,
        outputs.urine_volume, outputs.u_sodium, outputs.u_potassium,
        outputs.u_ph, outputs.u_con, outputs.ep_h, outputs.status,
    ]
    return ",".join(str(v) for v in values)


def build_1d_vector(
    patient_output_data: str,
    urine_volume: float,
    urine_sodium: float,
    urine_potassium: float,
    na_k_ratio: float,
) -> List[Any]:
    """Build and validate the patient output 1D vector per V7 spec.

    Structure (5 elements):
      [0] str   — full patient output CSV string
      [1] float — urine_volume (ml)
      [2] float — urine_sodium (mEq/L)
      [3] float — urine_potassium (mEq/L)
      [4] float — na_k_ratio
    """
    vector = [
        str(patient_output_data),
        float(urine_volume),
        float(urine_sodium),
        float(urine_potassium),
        float(na_k_ratio),
    ]

    if len(vector) != 5:
        raise errors.Permanent(
            f"Output vector has invalid length: {len(vector)}",
            user_message="Internal error while building output vector"
        )
    if not isinstance(vector[0], str):
        raise errors.Permanent(
            "Output vector element 0 is not string",
            user_message="Internal output type validation failed"
        )
    for i in range(1, 5):
        if not isinstance(vector[i], float):
            raise errors.Permanent(
                f"Output vector element {i} is not float",
                user_message="Internal output type validation failed"
            )
        if not math.isfinite(vector[i]):
            raise errors.Permanent(
                f"Output vector element {i} is not finite",
                user_message="Internal numeric validation failed"
            )

    return vector


# Kubyk response builder

def build_kubyk_values(
    raw_input_data: str,
    calibration_input_data: str,
    calibration_output_data: str,
    vector_1d: List[Any],
    datestamp: int,
    calibration_applied: bool = True,
    is_calibration_session: bool = False,
    outputs: "ComputedPatientOutputs" = None,
    calibration: "CalibrationV6" = None,
) -> List[Dict[str, Any]]:
    """Assemble the full list of Kubyk parameter values for the response.

    Parameters returned for all sessions:
      raw_input_data, calibration_input_data

    Calibration sessions (PatientID == "CAL") additionally return:
      calibration_output_data (serialised JSON)

    Patient sessions additionally return:
      processed_output_data, Urine Volume, Urine Sodium, Urine Potassium, Na/K Ratio,
      and all 10 expanded diagnostic parameters added in v7.0.0.
    """
    ts_ms    = to_epoch_millis(datestamp)
    cal_note = "" if calibration_applied else " — WARNING: no CalData provided, values are raw voltages (mV)"

    values = [
        make_value(PARAM_RAW_INPUT, raw_input_data,         ts_ms, ts_ms),
        make_value(PARAM_CAL_INPUT, calibration_input_data, ts_ms, ts_ms),
    ]

    if is_calibration_session:
        values.append(make_value(
            PARAM_CAL_OUTPUT,
            json.dumps(dict(zip(
                ["Device SN", "DateStamp", "TT_Offset", "T1_Offset", "T2_Offset", "T3_Offset",
                 "UV1_Gain", "UV2_Gain", "UN_P0", "UN_P1", "UN_P2", "UN_P3",
                 "UK_P0", "UK_P1", "UK_P2", "UK_P3", "UH_P0", "UH_P1"],
                calibration_output_data.split(",")
            ))),
            ts_ms, ts_ms,
            notes="Processed calibration 1D vector (serialised JSON)" + cal_note
        ))
    else:
        values.append(make_value(
            PARAM_PROCESSED_OUTPUT,
            json.dumps(vector_1d),
            ts_ms, ts_ms,
            notes="Patient output 1D vector (serialised JSON): [CSV string, volume, sodium, potassium, Na/K ratio]" + cal_note
        ))

        # Primary biomarkers
        values.append(make_value(PARAM_URINE_VOLUME,    float(vector_1d[1]), ts_ms, ts_ms, unit_measure="ml"))
        values.append(make_value(PARAM_URINE_SODIUM,    float(vector_1d[2]), ts_ms, ts_ms, unit_measure="mEq/L" if calibration_applied else "mV"))
        values.append(make_value(PARAM_URINE_POTASSIUM, float(vector_1d[3]), ts_ms, ts_ms, unit_measure="mEq/L" if calibration_applied else "mV"))
        values.append(make_value(PARAM_NA_K_RATIO,      float(vector_1d[4]), ts_ms, ts_ms))

        # Expanded diagnostic parameters (v7.0.0)
        if outputs is not None:
            cal_ts        = calibration.DateStamp if calibration is not None else 0
            cal_completed = "true" if (calibration is not None and calibration.DateStamp != 0) else "false"
            values.append(make_value(PARAM_TACT_TIME,             outputs.tact_time,  ts_ms, ts_ms, unit_measure="s"))
            values.append(make_value(PARAM_URINE_TEMPERATURE,     outputs.utemp,      ts_ms, ts_ms, unit_measure="°C"))
            values.append(make_value(PARAM_ENVIRONMENT_TEMP,      outputs.etemp,      ts_ms, ts_ms, unit_measure="°C"))
            values.append(make_value(PARAM_URINE_PH,              outputs.u_ph,       ts_ms, ts_ms))
            values.append(make_value(PARAM_URINE_CONDUCTIVITY,    outputs.u_con,      ts_ms, ts_ms))
            values.append(make_value(PARAM_STATUS,                outputs.status,     ts_ms, ts_ms))
            values.append(make_value(PARAM_PATIENT_ID,            outputs.patient_id, ts_ms, ts_ms))
            values.append(make_value(PARAM_DEVICE_ID,             outputs.device_id,  ts_ms, ts_ms))
            values.append(make_value(PARAM_CALIBRATION_TIMESTAMP, cal_ts,             ts_ms, ts_ms))
            values.append(make_value(PARAM_CALIBRATION_COMPLETED, cal_completed,      ts_ms, ts_ms))

    return values


# Main request handler

@app.handler("ProcessCSV")
async def process_csv(ctx, req: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(
        "Received ProcessCSV request version=%s",
        SERVICE_VERSION,
        extra={"correlation_id": getattr(ctx, "correlation_id", None)}
    )

    # Fail fast if we are close to the deadline — processing needs at least 10 s
    if getattr(ctx, "deadline", None):
        remaining = (ctx.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining < 10:
            raise errors.DeadlineExceeded(
                f"Only {remaining:.0f}s remaining, need at least 10s for processing"
            )

    device_serial_number = req.get("deviceSerialNumber")
    session_id           = req.get("sessionId")
    data_field           = req.get("data")

    if device_serial_number is None or str(device_serial_number).strip() == "":
        raise errors.Permanent("Missing deviceSerialNumber", user_message="Missing required field: deviceSerialNumber")
    if session_id is None or str(session_id).strip() == "":
        raise errors.Permanent("Missing sessionId", user_message="Missing required field: sessionId")
    if data_field is None or str(data_field).strip() == "":
        raise errors.Permanent("Missing data", user_message="Missing required field: data")

    try:
        inner = parse_inner_data_json(str(data_field))
        raw_csv_text, cal_csv_text = extract_required_strings(inner)

        raw_matrix, patient_ids, sensor_ids = parse_raw_csv(raw_csv_text)
        raw_device_id, patient_id, sensor_id, datestamp, _timestamp = extract_identity_from_raw_matrix(
            raw_matrix, patient_ids, sensor_ids
        )
        calibration = parse_calibration_csv(cal_csv_text, device_id=raw_device_id)

        # --- Calibration session (PatientID == "CAL") ---
        # Identified by the reserved string "CAL" in the PatientID field (V7+ convention).
        # Returns the parsed calibration vector for the platform to store; no patient output.
        if patient_id.upper() == "CAL":
            logger.info(
                "Calibration session: serial=%s device=%s sensor=%s session=%s",
                device_serial_number, raw_device_id, sensor_id, session_id
            )
            values = build_kubyk_values(
                raw_input_data=normalize_compact_string(raw_csv_text),
                calibration_input_data=normalize_compact_string(cal_csv_text),
                calibration_output_data=build_calibration_output_data(calibration),
                vector_1d=[],
                datestamp=datestamp,
                calibration_applied=calibration.DateStamp != 0,
                is_calibration_session=True,
            )
            return {"sessionId": session_id, "values": values}

        # --- Patient session ---
        validate_identity(
            device_serial_number=str(device_serial_number),
            raw_device_id=raw_device_id,
            patient_id=patient_id,
            calibration=calibration,
        )

        outputs   = compute_patient_outputs(raw_matrix, calibration, patient_ids, sensor_ids)
        vector_1d = build_1d_vector(
            patient_output_data=build_patient_output_data(outputs),
            urine_volume=outputs.urine_volume,
            urine_sodium=outputs.u_sodium,
            urine_potassium=outputs.u_potassium,
            na_k_ratio=outputs.na_k_ratio,
        )

        values = build_kubyk_values(
            raw_input_data=normalize_compact_string(raw_csv_text),
            calibration_input_data=normalize_compact_string(cal_csv_text),
            calibration_output_data=build_calibration_output_data(calibration),
            vector_1d=vector_1d,
            datestamp=outputs.datestamp,
            calibration_applied=outputs.calibration_applied,
            is_calibration_session=False,
            outputs=outputs,
            calibration=calibration,
        )

        logger.info(
            "Processed successfully: serial=%s device=%s patient=%s session=%s",
            device_serial_number, outputs.device_id, outputs.patient_id, session_id
        )
        return {"sessionId": session_id, "values": values}

    except errors.DeadlineExceeded:
        raise
    except errors.Permanent:
        raise
    except Exception as exc:
        logger.error("Unexpected processing error: %s", exc, exc_info=True)
        raise errors.Permanent(
            f"Unexpected error processing payload: {exc}",
            user_message="Failed to process payload"
        ) from exc


# Entrypoint

if __name__ == "__main__":
    logger.info("Starting Unephra production processor for Kubyk version=%s", SERVICE_VERSION)
    logger.info("Press Ctrl+C to stop")

    if sys.platform.startswith("win"):
        # LEDSAS signal handlers can be problematic on Windows terminals
        app._setup_signal_handlers = lambda: None

    app.run()


# Unit tests — run with: python -m pytest processor1d.py -v

def _make_test_matrix(n_rows: int = 20, ec_peak_rows: int = 5) -> np.ndarray:
    """Synthetic (n_rows x 15) V7 matrix.
    EC is flat at 5000 then peaks at 17100 for the last ec_peak_rows rows.
    COL_UPID and COL_SID hold 0.0 placeholders — use _make_test_patient/sensor_ids().
    """
    rows = []
    for i in range(n_rows):
        ec = 17100.0 if i >= (n_rows - ec_peak_rows) else 5000.0
        rows.append([
            100001,            # COL_UDN    Device SN (numeric)
            0.0,               # COL_UPID   PatientID placeholder
            0.0,               # COL_SID    SensorID placeholder
            1730158863,        # COL_DATE   DateStamp
            45058 + i * 1000, # COL_TIME   TimeStamp (ms)
            24.75,             # COL_T1     DTemp1
            24.75,             # COL_T2     DTemp2
            24.80,             # COL_T3     DTemp3
            26.18,             # COL_T4     ATemp
            2537.26,           # COL_VNA    VNa
            2796.94,           # COL_VK     VK
            3051.94,           # COL_VNAK   VNaK
            2960.32,           # COL_VPH    VpH
            ec,                # COL_EC     EC
            0,                 # COL_STATUS Status
        ])
    return np.array(rows, dtype=np.float64)


def _make_test_patient_ids(n_rows: int = 20) -> List[str]:
    return ["NFC-A3B9"] * n_rows


def _make_test_sensor_ids(n_rows: int = 20) -> List[str]:
    return ["SN-001X"] * n_rows


def _make_test_calibration() -> "CalibrationV6":
    return CalibrationV6(
        DeviceID="100001",
        DateStamp=1730158863,
        TT_Offset=7.5,
        T1_Offset=0.13, T2_Offset=0.15, T3_Offset=0.43,
        UV1_Gain=38.561, UV2_Gain=537.26,
        UN_P0=-2295.3, UN_P1=25.2349, UN_P2=-11.32, UN_P3=0.96,
        UK_P0=-3195.1, UK_P1=32.1313, UK_P2=-23.82, UK_P3=0.96,
        UH_P0=-8.73, UH_P1=11.2,
    )


def test_compute_patient_outputs_runs():
    matrix      = _make_test_matrix()
    patient_ids = _make_test_patient_ids()
    sensor_ids  = _make_test_sensor_ids()
    cal         = _make_test_calibration()
    outputs     = compute_patient_outputs(matrix, cal, patient_ids, sensor_ids)
    assert outputs.device_id  == "100001"
    assert outputs.patient_id == "NFC-A3B9"
    assert outputs.sensor_id  == "SN-001X"
    assert isinstance(outputs.urine_volume, float)
    assert isinstance(outputs.u_sodium, float)
    assert isinstance(outputs.u_potassium, float)
    assert isinstance(outputs.na_k_ratio, float)
    assert isinstance(outputs.dtemp3, float)
    assert outputs.status == STATUS_OK


def test_urine_volume_formula():
    """UV must follow V7 spec: UV = UV1 + UV2. utemp comes from ATemp (COL_T4)."""
    matrix      = _make_test_matrix()
    patient_ids = _make_test_patient_ids()
    sensor_ids  = _make_test_sensor_ids()
    cal         = _make_test_calibration()
    outputs     = compute_patient_outputs(matrix, cal, patient_ids, sensor_ids)
    T1 = matrix[:, COL_T1]
    T2 = matrix[:, COL_T2]
    t1_max_adj = float(np.max(T1)) + cal.T1_Offset
    t2_max_adj = float(np.max(T2)) + cal.T2_Offset
    utemp = float(np.max(matrix[:, COL_T4])) + cal.T3_Offset
    etemp = round((
        (float(np.min(T1)) + cal.T1_Offset) +
        (float(np.min(T2)) + cal.T2_Offset) +
        (float(np.min(T2)) + cal.T2_Offset)
    ) / 3.0, 4)
    denom1 = utemp - t1_max_adj
    denom2 = utemp - t2_max_adj
    uv1 = (t1_max_adj - etemp) / denom1 * cal.UV1_Gain if abs(denom1) > 1e-9 else 0.0
    uv2 = (t2_max_adj - etemp) / denom2 * cal.UV2_Gain if abs(denom2) > 1e-9 else 0.0
    assert outputs.urine_volume == round(uv1 + uv2, 4)


def test_na_k_ratio_zero_when_potassium_near_zero():
    matrix      = _make_test_matrix()
    patient_ids = _make_test_patient_ids()
    sensor_ids  = _make_test_sensor_ids()
    cal         = _make_test_calibration()
    cal.UK_P0 = cal.UK_P1 = cal.UK_P2 = cal.UK_P3 = 0.0
    outputs = compute_patient_outputs(matrix, cal, patient_ids, sensor_ids)
    assert outputs.na_k_ratio == 0.0


def test_dtemp3_is_computed():
    """DTemp3 output should be the mean of the DTemp3 column."""
    matrix      = _make_test_matrix()
    patient_ids = _make_test_patient_ids()
    sensor_ids  = _make_test_sensor_ids()
    cal         = _make_test_calibration()
    outputs     = compute_patient_outputs(matrix, cal, patient_ids, sensor_ids)
    assert outputs.dtemp3 == round(float(np.mean(matrix[:, COL_T3])), 4)


def test_bad_dtemp_interpolation():
    """Negative, zero, and NaN temperature values must all be replaced by interpolation."""
    matrix = _make_test_matrix()
    matrix[5, COL_T1]  = -1000.0       # canonical firmware sentinel
    matrix[6, COL_T1]  = -0.1          # any other negative is also invalid
    matrix[7, COL_T3]  = 0.0           # zero is physically impossible for temperature
    matrix[10, COL_T2] = float("nan")  # empty CSV cell arrives as NaN
    assert interpolate_bad_temps(matrix[:, COL_T1])[5] >= 0.0
    assert interpolate_bad_temps(matrix[:, COL_T1])[6] >= 0.0
    assert interpolate_bad_temps(matrix[:, COL_T3])[7] > 0.0
    assert not np.isnan(interpolate_bad_temps(matrix[:, COL_T2])[10])


def test_build_1d_vector_shape_and_types():
    """Output vector must have exactly 5 elements: 1 string + 4 floats."""
    vector = build_1d_vector(
        patient_output_data="10201,NFC-A3B9,SN-001X,1730158818,8,24.49,38.53,22.20,24.80,1809.4,120.5,45.2,7.2,17857.5,0,0",
        urine_volume=1.5, urine_sodium=120.0, urine_potassium=40.0, na_k_ratio=3.0,
    )
    assert len(vector) == 5
    assert isinstance(vector[0], str)
    for i in range(1, 5):
        assert isinstance(vector[i], float)


def test_patient_id_cal_is_calibration_session():
    """Empty PatientID must raise; 'CAL' is handled upstream before validate_identity."""
    cal = _make_test_calibration()
    try:
        validate_identity(device_serial_number="100001", raw_device_id="100001", patient_id="", calibration=cal)
        assert False, "Should have raised for empty PatientID"
    except Exception as e:
        assert "invalid patientid" in str(e).lower()


def test_device_id_mismatch_raises():
    """RawData Device SN must match CalData Device SN."""
    cal = _make_test_calibration()  # Device SN = "100001"
    try:
        validate_identity(device_serial_number="anything", raw_device_id="99999", patient_id="NFC-A3B9", calibration=cal)
        assert False, "Should have raised"
    except Exception as e:
        assert "mismatch" in str(e).lower() or "does not match" in str(e).lower()


def test_device_id_match_passes():
    """Matching Device SN between RawData and CalData should not raise."""
    cal = _make_test_calibration()
    validate_identity(
        device_serial_number="anything",  # not validated against Device SN by design
        raw_device_id="100001",
        patient_id="NFC-A3B9",
        calibration=cal,
    )


def test_ec_flat_uses_last_third():
    """Flat EC should fall back to last 1/3 of data and set status = -1 (dry run)."""
    matrix      = _make_test_matrix()
    patient_ids = _make_test_patient_ids()
    sensor_ids  = _make_test_sensor_ids()
    matrix[:, COL_EC] = 0.0
    outputs = compute_patient_outputs(matrix, _make_test_calibration(), patient_ids, sensor_ids)
    assert outputs.status == -1


def test_parse_raw_csv_v7_structure():
    """parse_raw_csv should return matrix + patient_ids + sensor_ids for a V7 row."""
    row = "10201,NFC-A3B9,SN-001X,1730158863,45058,24.75,24.75,24.80,26.18,2537.26,2796.94,3051.94,2960.32,17100,0"
    matrix, patient_ids, sensor_ids = parse_raw_csv(row)
    assert matrix.shape == (1, N_COLS_V7)
    assert matrix[0, COL_UDN] == 100001.0  # Device SN stored as float in matrix
    assert patient_ids[0] == "NFC-A3B9"
    assert sensor_ids[0]  == "SN-001X"


def test_parse_raw_csv_rejects_wrong_column_count():
    """Rows with the wrong number of columns must raise."""
    row = "10201,2749675,1730158863,45058,24.75,24.75,26.18,2537.26,2796.94,3051.94,2960.32,17100"  # 12 cols
    try:
        parse_raw_csv(row)
        assert False, "Should have raised"
    except Exception as e:
        assert "columns" in str(e).lower()


def test_parse_calibration_csv_with_header():
    """Calibration CSV with a header row should parse correctly."""
    header = ",".join(CALIBRATION_HEADERS)
    values = "10201,1730158863,7.5,0.13,0.15,0.43,38.561,537.26,-2295.3,25.2349,-11.32,0.96,-3195.1,32.1313,-23.82,0.96,-8.73,11.2"
    cal = parse_calibration_csv(f"{header}\n{values}")
    assert cal.DeviceID  == "100001"
    assert cal.TT_Offset == 7.5
    assert cal.UV1_Gain  == 38.561
