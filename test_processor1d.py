"""
Standalone test suite for processor1d.py — v7
==============================================
Runs without kbm_ledsas_sdk installed by mocking it before import.

Usage:
    pip install pytest numpy
    python -m pytest test_processor1d.py -v

Or run directly:
    python test_processor1d.py
"""

import sys
import types
import math
import csv
import io
from unittest.mock import MagicMock

# Mock kbm_ledsas_sdk — allows running without the private SDK

class _PermanentError(Exception):
    def __init__(self, msg, user_message=None):
        super().__init__(msg)
        self.user_message = user_message

class _DeadlineExceededError(Exception):
    pass

_errors_mock = types.ModuleType("kbm_ledsas_sdk.errors")
_errors_mock.Permanent = _PermanentError
_errors_mock.DeadlineExceeded = _DeadlineExceededError

_sdk_mock = types.ModuleType("kbm_ledsas_sdk")
_sdk_mock.ServiceApp = MagicMock(return_value=MagicMock(handler=lambda name: (lambda f: f)))
_sdk_mock.errors = _errors_mock

sys.modules["kbm_ledsas_sdk"] = _sdk_mock
sys.modules["kbm_ledsas_sdk.errors"] = _errors_mock

import processor1d as p
import numpy as np

# Test helpers

REAL_CSV_PATH = "RawData.csv"
REAL_CAL_PATH = "CalData.csv"


def _make_matrix(n_rows=20, ec_peak_rows=5):
    """Synthetic V7 matrix (n_rows x 15). COL_UPID and COL_SID are 0.0 placeholders."""
    rows = []
    for i in range(n_rows):
        ec = 17100.0 if i >= (n_rows - ec_peak_rows) else 5000.0
        rows.append([
            100001,            # COL_UDN    Device SN (numeric)
            0.0,               # COL_UPID   PatientID placeholder
            0.0,               # COL_SID    SensorID placeholder
            1730158863,        # COL_DATE
            45058 + i * 1000, # COL_TIME
            24.75,             # COL_T1
            24.75,             # COL_T2
            24.80,             # COL_T3
            26.18,             # COL_T4
            2537.26,           # COL_VNA
            2796.94,           # COL_VK
            3051.94,           # COL_VNAK
            2960.32,           # COL_VPH
            ec,                # COL_EC
            0,                 # COL_STATUS
        ])
    return np.array(rows, dtype=float)


def _make_patient_ids(n=20):
    return ["NFC-A3B9"] * n


def _make_sensor_ids(n=20):
    return ["SN-001"] * n


def _make_cal():
    return p.CalibrationV6(
        DeviceID="100001", DateStamp=1730158863,
        TT_Offset=7.5, T1_Offset=0.13, T2_Offset=0.15, T3_Offset=0.43,
        UV1_Gain=38.561, UV2_Gain=537.26,
        UN_P0=-2295.3,  UN_P1=25.2349, UN_P2=-11.32, UN_P3=0.96,
        UK_P0=-3195.1,  UK_P1=32.1313, UK_P2=-23.82, UK_P3=0.96,
        UH_P0=-8.73,    UH_P1=11.2,
    )


# compute_patient_outputs

def test_compute_runs_and_returns_correct_types():
    outputs = p.compute_patient_outputs(_make_matrix(), _make_cal(), _make_patient_ids(), _make_sensor_ids())
    assert outputs.device_id  == "100001"
    assert outputs.patient_id == "NFC-A3B9"
    assert outputs.sensor_id  == "SN-001"
    assert isinstance(outputs.urine_volume, float)
    assert isinstance(outputs.u_sodium, float)
    assert isinstance(outputs.u_potassium, float)
    assert isinstance(outputs.na_k_ratio, float)
    assert isinstance(outputs.dtemp3, float)
    assert outputs.status == p.STATUS_OK
    assert outputs.calibration_applied is True
    print("  PASS test_compute_runs_and_returns_correct_types")


def test_urine_volume_formula_matches_spec():
    """UV = UV1 + UV2; utemp comes from ATemp (COL_T4)."""
    matrix  = _make_matrix()
    cal     = _make_cal()
    outputs = p.compute_patient_outputs(matrix, cal, _make_patient_ids(), _make_sensor_ids())
    T1 = matrix[:, p.COL_T1]; T2 = matrix[:, p.COL_T2]
    t1a = float(np.max(T1)) + cal.T1_Offset
    t2a = float(np.max(T2)) + cal.T2_Offset
    ut  = float(np.max(matrix[:, p.COL_T4])) + cal.T3_Offset
    et  = round(((float(np.min(T1)) + cal.T1_Offset) + (float(np.min(T2)) + cal.T2_Offset) * 2) / 3.0, 4)
    d1  = ut - t1a; d2 = ut - t2a
    uv1 = (t1a - et) / d1 * cal.UV1_Gain if abs(d1) > 1e-9 else 0.0
    uv2 = (t2a - et) / d2 * cal.UV2_Gain if abs(d2) > 1e-9 else 0.0
    assert outputs.urine_volume == round(uv1 + uv2, 4)
    print("  PASS test_urine_volume_formula_matches_spec")


def test_na_k_ratio_zero_when_potassium_zero():
    cal = _make_cal()
    cal.UK_P0 = cal.UK_P1 = cal.UK_P2 = cal.UK_P3 = 0.0
    outputs = p.compute_patient_outputs(_make_matrix(), cal, _make_patient_ids(), _make_sensor_ids())
    assert outputs.na_k_ratio == 0.0
    print("  PASS test_na_k_ratio_zero_when_potassium_zero")


def test_dtemp3_is_mean_of_column():
    matrix  = _make_matrix()
    outputs = p.compute_patient_outputs(matrix, _make_cal(), _make_patient_ids(), _make_sensor_ids())
    assert outputs.dtemp3 == round(float(np.mean(matrix[:, p.COL_T3])), 4)
    print("  PASS test_dtemp3_is_mean_of_column")


def test_ec_flat_returns_dry_run_status():
    """Flat EC should use last 1/3 of data and set status = -1."""
    matrix = _make_matrix()
    matrix[:, p.COL_EC] = 0.0
    outputs = p.compute_patient_outputs(matrix, _make_cal(), _make_patient_ids(), _make_sensor_ids())
    assert outputs.status == -1
    print("  PASS test_ec_flat_returns_dry_run_status")


# interpolate_bad_temps — negative, zero, and NaN (empty cell)

def test_bad_dtemp_negative_interpolated():
    matrix = _make_matrix()
    matrix[5, p.COL_T1]  = -1000.0  # canonical firmware sentinel
    matrix[6, p.COL_T1]  = -0.1     # any other negative
    result = p.interpolate_bad_temps(matrix[:, p.COL_T1])
    assert result[5] >= 0.0
    assert result[6] >= 0.0
    assert abs(result[5] - 24.75) < 0.01
    print("  PASS test_bad_dtemp_negative_interpolated")


def test_bad_dtemp_zero_interpolated():
    matrix = _make_matrix()
    matrix[7, p.COL_T3] = 0.0
    result = p.interpolate_bad_temps(matrix[:, p.COL_T3])
    assert result[7] > 0.0
    print("  PASS test_bad_dtemp_zero_interpolated")


def test_bad_dtemp_nan_interpolated():
    """NaN represents an empty cell from the CSV parser."""
    matrix = _make_matrix()
    matrix[10, p.COL_T2] = float("nan")
    result = p.interpolate_bad_temps(matrix[:, p.COL_T2])
    assert not np.isnan(result[10])
    assert result[10] >= 0.0
    print("  PASS test_bad_dtemp_nan_interpolated")


# Identity calibration (no CalData)

def test_identity_calibration_flag_is_false():
    cal = p.CalibrationV6.identity(device_id="100001")
    outputs = p.compute_patient_outputs(_make_matrix(), cal, _make_patient_ids(), _make_sensor_ids())
    assert outputs.calibration_applied is False
    print("  PASS test_identity_calibration_flag_is_false")


def test_identity_calibration_sodium_equals_voltage():
    """Without CalData, USodium should equal raw VNa average (identity passthrough)."""
    matrix  = _make_matrix()
    cal     = p.CalibrationV6.identity(device_id="100001")
    outputs = p.compute_patient_outputs(matrix, cal, _make_patient_ids(), _make_sensor_ids())
    EC      = matrix[:, p.COL_EC]
    mask_10 = (EC >= 0.98 * float(np.max(EC))).astype(float)
    expected = p.nonzero_avg(matrix[:, p.COL_VNA] * mask_10)
    assert abs(outputs.u_sodium - expected) < 1e-6
    print("  PASS test_identity_calibration_sodium_equals_voltage")


def test_no_cal_sentinel_returns_identity_cal():
    cal = p.parse_calibration_csv(p.NO_CALIBRATION_SENTINEL, device_id="100001")
    assert cal.DeviceID  == "100001"
    assert cal.DateStamp == 0
    assert cal.UV1_Gain  == 0.0
    assert cal.UN_P1     == 1.0
    print("  PASS test_no_cal_sentinel_returns_identity_cal")


# build_1d_vector

def test_vector_has_5_elements_correct_types():
    vector = p.build_1d_vector(
        patient_output_data="100001,NFC-A3B9,SN-001,1730158818,8,24.49,38.53,22.20,24.80,1809.4,120.5,45.2,7.2,17857.5,0,0",
        urine_volume=1.5, urine_sodium=120.0, urine_potassium=40.0, na_k_ratio=3.0,
    )
    assert len(vector) == 5
    assert isinstance(vector[0], str)
    for i in range(1, 5):
        assert isinstance(vector[i], float)
    print("  PASS test_vector_has_5_elements_correct_types")


# validate_identity

def test_patient_id_empty_raises():
    """Empty PatientID must raise — CAL sessions are handled upstream."""
    try:
        p.validate_identity("100001", raw_device_id="100001", patient_id="", calibration=_make_cal())
        assert False, "Should have raised"
    except _PermanentError as e:
        assert "invalid patientid" in str(e).lower()
    print("  PASS test_patient_id_empty_raises")


def test_device_sn_mismatch_raises():
    cal = _make_cal()  # Device SN = "100001"
    try:
        p.validate_identity("anything", raw_device_id="99999", patient_id="NFC-A3B9", calibration=cal)
        assert False, "Should have raised"
    except _PermanentError as e:
        assert "mismatch" in str(e).lower() or "does not match" in str(e).lower()
    print("  PASS test_device_sn_mismatch_raises")


def test_device_sn_match_passes():
    """deviceSerialNumber is not validated against Device SN by design."""
    p.validate_identity("anything", raw_device_id="100001", patient_id="NFC-A3B9", calibration=_make_cal())
    print("  PASS test_device_sn_match_passes")


# parse_raw_csv — V7 (15 columns)

def test_parse_raw_csv_v7_structure():
    row = "100001,NFC-A3B9,SN-001,1730158863,45058,24.75,24.75,24.80,26.18,2537.26,2796.94,3051.94,2960.32,17100,0"
    matrix, patient_ids, sensor_ids = p.parse_raw_csv(row)
    assert matrix.shape == (1, p.N_COLS_V7)
    assert matrix[0, p.COL_UDN] == 100001.0
    assert patient_ids[0] == "NFC-A3B9"
    assert sensor_ids[0]  == "SN-001"
    print("  PASS test_parse_raw_csv_v7_structure")


def test_parse_raw_csv_tolerates_trailing_ref():
    """Trailing ,, or #REF! columns beyond N_COLS_V7 must be stripped."""
    row = "100001,NFC-A3B9,SN-001,1730158863,45058,24.75,24.75,24.80,26.18,2537.26,2796.94,3051.94,2960.32,17100,0,,#REF!"
    matrix, patient_ids, sensor_ids = p.parse_raw_csv(row)
    assert matrix.shape == (1, p.N_COLS_V7)
    print("  PASS test_parse_raw_csv_tolerates_trailing_ref")


def test_parse_raw_csv_rejects_spreadsheet_artifact_in_data():
    """#REF! inside the real columns must be rejected."""
    row = "100001,NFC-A3B9,SN-001,1730158863,45058,24.75,24.75,24.80,#REF!,2537.26,2796.94,3051.94,2960.32,17100,0"
    try:
        p.parse_raw_csv(row)
        assert False, "Should have raised"
    except _PermanentError as e:
        assert "artifact" in str(e).lower()
    print("  PASS test_parse_raw_csv_rejects_spreadsheet_artifact_in_data")


def test_parse_raw_csv_empty_temp_cell_becomes_nan():
    """Empty cell in a temperature column must be stored as NaN, not raise."""
    # COL_T3 is position 7 — leave it empty
    row = "100001,NFC-A3B9,SN-001,1730158863,45058,24.75,24.75,,26.18,2537.26,2796.94,3051.94,2960.32,17100,0"
    matrix, _, _ = p.parse_raw_csv(row)
    assert np.isnan(matrix[0, p.COL_T3])
    print("  PASS test_parse_raw_csv_empty_temp_cell_becomes_nan")


def test_parse_raw_csv_rejects_wrong_column_count():
    row = "100001,2749675,1730158863,45058,24.75,24.75,26.18,2537.26,2796.94,3051.94,2960.32,17100"  # 12 cols
    try:
        p.parse_raw_csv(row)
        assert False, "Should have raised"
    except _PermanentError as e:
        assert "columns" in str(e).lower()
    print("  PASS test_parse_raw_csv_rejects_wrong_column_count")


def test_parse_raw_csv_real_file():
    import os
    if not os.path.exists(REAL_CSV_PATH):
        print(f"  SKIP test_parse_raw_csv_real_file (file not found: {REAL_CSV_PATH})")
        return
    with open(REAL_CSV_PATH) as f:
        text = f.read()
    matrix, patient_ids, sensor_ids = p.parse_raw_csv(text)
    assert matrix.ndim == 2
    assert matrix.shape[1] == p.N_COLS_V7
    assert matrix.shape[0] >= 1
    print(f"  PASS test_parse_raw_csv_real_file ({matrix.shape[0]} rows parsed)")


# parse_calibration_csv

def test_parse_calibration_with_header():
    cal_text = (
        "Device SN,DateStamp,TT_Offset,T1_Offset,T2_Offset,T3_Offset,"
        "UV1_Gain,UV2_Gain,UN_P0,UN_P1,UN_P2,UN_P3,"
        "UK_P0,UK_P1,UK_P2,UK_P3,UH_P0,UH_P1\n"
        "100001,1730158863,7.5,0.13,0.15,0.43,38.561,537.26,"
        "-2295.3,25.2349,-11.32,0.96,-3195.1,32.1313,-23.82,0.96,-8.73,11.2"
    )
    cal = p.parse_calibration_csv(cal_text)
    assert cal.DeviceID  == "100001"
    assert cal.TT_Offset == 7.5
    assert cal.UV1_Gain  == 38.561
    print("  PASS test_parse_calibration_with_header")


def test_parse_calibration_without_header():
    values = "100001,1730158863,7.5,0.13,0.15,0.43,38.561,537.26,-2295.3,25.2349,-11.32,0.96,-3195.1,32.1313,-23.82,0.96,-8.73,11.2"
    cal = p.parse_calibration_csv(values)
    assert cal.DeviceID  == "100001"
    assert cal.UV2_Gain  == 537.26
    print("  PASS test_parse_calibration_without_header")


# Full pipeline tests with real CSV files (skipped if files not present)

def test_full_pipeline_real_csv_with_caldata():
    import os
    if not os.path.exists(REAL_CSV_PATH) or not os.path.exists(REAL_CAL_PATH):
        print(f"  SKIP test_full_pipeline_real_csv_with_caldata (CSV files not found)")
        return
    with open(REAL_CSV_PATH) as f: raw_csv = f.read()
    with open(REAL_CAL_PATH) as f: cal_csv = f.read()

    matrix, patient_ids, sensor_ids = p.parse_raw_csv(raw_csv)
    calibration = p.parse_calibration_csv(cal_csv)
    raw_device_id, patient_id, sensor_id, datestamp, _ = p.extract_identity_from_raw_matrix(
        matrix, patient_ids, sensor_ids
    )
    p.validate_identity(raw_device_id, raw_device_id, patient_id, calibration)
    outputs = p.compute_patient_outputs(matrix, calibration, patient_ids, sensor_ids)
    vector  = p.build_1d_vector(
        patient_output_data=p.build_patient_output_data(outputs),
        urine_volume=outputs.urine_volume, urine_sodium=outputs.u_sodium,
        urine_potassium=outputs.u_potassium, na_k_ratio=outputs.na_k_ratio,
    )
    assert len(vector) == 5
    assert isinstance(vector[0], str)
    assert math.isfinite(vector[4])
    assert outputs.calibration_applied is True
    print(f"  PASS test_full_pipeline_real_csv_with_caldata")
    print(f"    UTemp={outputs.utemp} ETemp={outputs.etemp} UCon={outputs.u_con} NaK={outputs.na_k_ratio}")


def test_full_pipeline_real_csv_no_caldata():
    import os
    if not os.path.exists(REAL_CSV_PATH):
        print(f"  SKIP test_full_pipeline_real_csv_no_caldata (file not found: {REAL_CSV_PATH})")
        return
    with open(REAL_CSV_PATH) as f: raw_csv = f.read()

    matrix, patient_ids, sensor_ids = p.parse_raw_csv(raw_csv)
    raw_device_id2, _, _, _, _ = p.extract_identity_from_raw_matrix(matrix, patient_ids, sensor_ids)
    calibration = p.parse_calibration_csv(p.NO_CALIBRATION_SENTINEL, device_id=raw_device_id2)
    outputs     = p.compute_patient_outputs(matrix, calibration, patient_ids, sensor_ids)
    vector      = p.build_1d_vector(
        patient_output_data=p.build_patient_output_data(outputs),
        urine_volume=outputs.urine_volume, urine_sodium=outputs.u_sodium,
        urine_potassium=outputs.u_potassium, na_k_ratio=outputs.na_k_ratio,
    )
    assert len(vector) == 5
    assert outputs.calibration_applied is False
    assert outputs.urine_volume == 0.0  # no gains without calibration
    assert math.isfinite(outputs.u_sodium)
    print(f"  PASS test_full_pipeline_real_csv_no_caldata")
    print(f"    USodium(raw mV)={outputs.u_sodium} UPotassium(raw mV)={outputs.u_potassium} NaK={outputs.na_k_ratio}")


# Runner


# interpolate_bad_temps — edge cases

def test_bad_dtemp_first_value_edge():
    """Bad value at index 0 must be forward-filled from nearest valid value."""
    arr = np.array([float("nan"), 24.75, 24.75, 24.75, 24.75], dtype=np.float64)
    result = p.interpolate_bad_temps(arr)
    assert result[0] == 24.75
    print("  PASS test_bad_dtemp_first_value_edge")


def test_bad_dtemp_last_value_edge():
    """Bad value at last index must be backward-filled from nearest valid value."""
    arr = np.array([24.75, 24.75, 24.75, 24.75, -1000.0], dtype=np.float64)
    result = p.interpolate_bad_temps(arr)
    assert result[-1] == 24.75
    print("  PASS test_bad_dtemp_last_value_edge")


def test_bad_dtemp_multiple_consecutive():
    """Multiple consecutive bad values at start must all be forward-filled."""
    arr = np.array([-1000.0, 0.0, float("nan"), 24.75, 24.75], dtype=np.float64)
    result = p.interpolate_bad_temps(arr)
    assert all(result[:3] == 24.75)
    print("  PASS test_bad_dtemp_multiple_consecutive")


# parse_calibration_csv — missing headers

def test_parse_calibration_missing_headers_raises():
    """CalData with missing required headers must raise."""
    incomplete = "Device SN,DateStamp,TT_Offset\n100001,1730158863,7.5"
    try:
        p.parse_calibration_csv(incomplete)
        assert False, "Should have raised"
    except _PermanentError as e:
        assert "missing" in str(e).lower()
    print("  PASS test_parse_calibration_missing_headers_raises")


# compute_patient_outputs — status from first row

def test_status_taken_from_first_row():
    """Session status must come from STATUS column of the first row."""
    matrix = _make_matrix()
    matrix[0, p.COL_STATUS] = 3
    matrix[1, p.COL_STATUS] = 0
    outputs = p.compute_patient_outputs(matrix, _make_cal(), _make_patient_ids(), _make_sensor_ids())
    assert outputs.status == 3
    print("  PASS test_status_taken_from_first_row")


# normalize_compact_string — CRLF and row separation

def test_normalize_compact_string_crlf():
    """normalize_compact_string must use \r\n as row separator."""
    sample = "Device SN,PatientID\n100001,NFC-001\n100001,NFC-002"
    result = p.normalize_compact_string(sample)
    assert "\r\n" in result
    print("  PASS test_normalize_compact_string_crlf")


def test_normalize_compact_string_rows_separated():
    """Each row must appear on its own line, not joined by spaces."""
    sample = "Device SN,PatientID\n100001,NFC-001\n100001,NFC-002"
    result = p.normalize_compact_string(sample)
    rows = result.split("\r\n")
    assert len(rows) == 3
    assert rows[0] == "Device SN,PatientID"
    assert rows[1].startswith("100001")
    print("  PASS test_normalize_compact_string_rows_separated")


# build_kubyk_values — parameter counts, keys, units

def _make_kubyk_patient_values(cal=None):
    matrix  = _make_matrix()
    c       = cal if cal else _make_cal()
    outputs = p.compute_patient_outputs(matrix, c, _make_patient_ids(), _make_sensor_ids())
    vector_1d = p.build_1d_vector(
        patient_output_data=p.build_patient_output_data(outputs),
        urine_volume=outputs.urine_volume, urine_sodium=outputs.u_sodium,
        urine_potassium=outputs.u_potassium, na_k_ratio=outputs.na_k_ratio,
    )
    values = p.build_kubyk_values(
        raw_input_data="raw", calibration_input_data="cal",
        calibration_output_data="cal_out", vector_1d=vector_1d,
        datestamp=outputs.datestamp,
        calibration_applied=(c.DateStamp != 0),
        is_calibration_session=False, outputs=outputs, calibration=c,
    )
    return values, outputs


def test_build_kubyk_values_patient_session_17_params():
    """Patient session must return exactly 17 Kubyk parameters."""
    values, _ = _make_kubyk_patient_values()
    assert len(values) == 17
    print("  PASS test_build_kubyk_values_patient_session_17_params")


def test_build_kubyk_values_calibration_session_3_params():
    """Calibration session must return exactly 3 Kubyk parameters."""
    values = p.build_kubyk_values(
        raw_input_data="raw", calibration_input_data="cal",
        calibration_output_data="cal_out", vector_1d=[],
        datestamp=1730158863, calibration_applied=True,
        is_calibration_session=True,
    )
    assert len(values) == 3
    assert {v["parameterKey"] for v in values} == {
        "raw_input_data", "calibration_input_data", "calibration_output_data"
    }
    print("  PASS test_build_kubyk_values_calibration_session_3_params")


def test_build_kubyk_values_device_sn_key():
    """Device parameter must use key 'Device SN', not 'Device ID'."""
    values, _ = _make_kubyk_patient_values()
    keys = {v["parameterKey"] for v in values}
    assert "Device SN" in keys
    assert "Device ID" not in keys
    print("  PASS test_build_kubyk_values_device_sn_key")


def test_build_kubyk_values_unit_meql_with_cal():
    """Sodium and Potassium must use mEq/L when calibration is applied."""
    values, _ = _make_kubyk_patient_values()
    sodium    = next(v for v in values if v["parameterKey"] == "Urine Sodium")
    potassium = next(v for v in values if v["parameterKey"] == "Urine Potassium")
    assert sodium["unitMeasure"] == "mEq/L"
    assert potassium["unitMeasure"] == "mEq/L"
    print("  PASS test_build_kubyk_values_unit_meql_with_cal")


def test_build_kubyk_values_unit_mv_without_cal():
    """Sodium and Potassium must use mV when no calibration applied."""
    values, _ = _make_kubyk_patient_values(cal=p.CalibrationV6.identity(device_id="100001"))
    sodium    = next(v for v in values if v["parameterKey"] == "Urine Sodium")
    potassium = next(v for v in values if v["parameterKey"] == "Urine Potassium")
    assert sodium["unitMeasure"] == "mV"
    assert potassium["unitMeasure"] == "mV"
    print("  PASS test_build_kubyk_values_unit_mv_without_cal")


def test_build_kubyk_values_calibration_completed():
    """Calibration Completed must be 'true' with cal, 'false' without."""
    values, _  = _make_kubyk_patient_values()
    completed  = next(v for v in values if v["parameterKey"] == "Calibration Completed")
    assert completed["value"] == "true"

    values2, _ = _make_kubyk_patient_values(cal=p.CalibrationV6.identity(device_id="100001"))
    completed2 = next(v for v in values2 if v["parameterKey"] == "Calibration Completed")
    assert completed2["value"] == "false"
    print("  PASS test_build_kubyk_values_calibration_completed")


if __name__ == "__main__":
    import types as _types
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and isinstance(v, _types.FunctionType)]

    passed = 0; failed = 0
    print(f"\nRunning {len(tests)} tests...\n")
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    if failed == 0:
        print("All tests passed. Code is ready for production.")
    else:
        print("Fix failing tests before deploying.")
    print("=" * 40)
