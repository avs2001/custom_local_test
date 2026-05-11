#!/usr/bin/env python3
"""
demo_processor.py
=================
Demonstrates the full processor1d.py pipeline using real device files.
Simulates exactly what happens when Kubyk sends a request.

Usage:
    # With CalData
    python demo_processor.py --raw RawData.csv --cal CalData.csv

    # Without CalData (returns raw voltages)
    python demo_processor.py --raw RawData.csv

Requirements:
    - processor1d.py in the same folder
    - pip install numpy
"""

import sys
import types
import json
import argparse
from unittest.mock import MagicMock


# Mock kbm_ledsas_sdk — allows running without the private SDK

class _PermanentError(Exception):
    def __init__(self, msg, user_message=None): super().__init__(msg)
class _DeadlineExceededError(Exception): pass

_errors_mock = types.ModuleType("kbm_ledsas_sdk.errors")
_errors_mock.Permanent = _PermanentError
_errors_mock.DeadlineExceeded = _DeadlineExceededError
_sdk_mock = types.ModuleType("kbm_ledsas_sdk")
_sdk_mock.ServiceApp = MagicMock(return_value=MagicMock(handler=lambda name: (lambda f: f)))
_sdk_mock.errors = _errors_mock
sys.modules["kbm_ledsas_sdk"] = _sdk_mock
sys.modules["kbm_ledsas_sdk.errors"] = _errors_mock

import processor1d as p


def run(raw_csv_path, cal_csv_path=None):
    print("=" * 65)
    print(f"Unephra Processor Demo  |  v{p.SERVICE_VERSION}")
    print("=" * 65)

    # Load files
    try:
        raw_csv = open(raw_csv_path).read()
        print(f"\nRawData  : {raw_csv_path}")
    except FileNotFoundError:
        print(f"\nERROR: File not found: {raw_csv_path}")
        sys.exit(1)

    if cal_csv_path:
        try:
            cal_csv = open(cal_csv_path).read()
            print(f"CalData  : {cal_csv_path}")
        except FileNotFoundError:
            print(f"WARNING  : CalData file not found — using identity calibration")
            cal_csv = ""
    else:
        print(f"CalData  : not provided — using identity calibration (raw voltages)")
        cal_csv = ""

    # Build Kubyk request
    print(f"\n{'─'*65}")
    print("Kubyk request (what the device sends)")
    kubyk_request = {
        "deviceSerialNumber": "100001",
        "sessionId": "demo-session-001",
        "data": json.dumps({
            "raw_input_data": raw_csv,
            "calibration_input_data": cal_csv
        })
    }
    print(f"  deviceSerialNumber : {kubyk_request['deviceSerialNumber']}")
    print(f"  sessionId          : {kubyk_request['sessionId']}")
    print(f"  data               : JSON with raw_input_data + calibration_input_data")

    # Decode request
    print(f"\n{'─'*65}")
    print("STEP 1 — Decode request  →  extract RawData and CalData")
    inner = p.parse_inner_data_json(kubyk_request["data"])
    raw_csv_text, cal_csv_text = p.extract_required_strings(inner)
    print(f"  raw_input_data         : extracted")
    print(f"  calibration_input_data : {'extracted' if cal_csv_text != p.NO_CALIBRATION_SENTINEL else 'missing — identity calibration'}")

    # Parse RawData
    print(f"\n{'─'*65}")
    print("STEP 2 — Parse RawData  →  2D matrix (N rows x 15 columns)")
    raw_matrix, patient_ids, sensor_ids = p.parse_raw_csv(raw_csv_text)
    print(f"  Shape    : {raw_matrix.shape[0]} rows x {raw_matrix.shape[1]} columns")
    print(f"  EC max   : {float(raw_matrix[:, p.COL_EC].max())}")

    # Identity + CalData
    print(f"\n{'─'*65}")
    print("STEP 3 — Extract identity + parse CalData")
    raw_device_id, patient_id, sensor_id, datestamp, _ = p.extract_identity_from_raw_matrix(
        raw_matrix, patient_ids, sensor_ids
    )
    calibration = p.parse_calibration_csv(cal_csv_text, device_id=raw_device_id)
    print(f"  Device SN  : {raw_device_id}")
    print(f"  PatientID  : {patient_id} {'→ calibration session' if patient_id.upper() == 'CAL' else '→ patient session'}")
    print(f"  SensorID   : {sensor_id}")
    print(f"  Cal applied: {calibration.DateStamp != 0} {'' if calibration.DateStamp != 0 else '(identity — no CalData)'}")

    # Calibration session path
    if patient_id.upper() == "CAL":
        print(f"\n{'─'*65}")
        print("CALIBRATION SESSION — PatientID == 'CAL'")
        cal_out = p.build_calibration_output_data(calibration)
        values = p.build_kubyk_values(
            raw_input_data=p.normalize_compact_string(raw_csv_text),
            calibration_input_data=p.normalize_compact_string(cal_csv_text),
            calibration_output_data=cal_out,
            vector_1d=[],
            datestamp=datestamp,
            calibration_applied=calibration.DateStamp != 0,
            is_calibration_session=True,
        )
        print(f"\nResponse parameters: {len(values)}")
        for v in values:
            print(f"  {v['parameterKey']}")
        print(f"\n{'='*65}")
        print("SUCCESS — Calibration session")
        print(f"{'='*65}")
        return

    # Validate identity
    print(f"\n{'─'*65}")
    print("STEP 4 — Validate identity")
    p.validate_identity(kubyk_request["deviceSerialNumber"], raw_device_id, patient_id, calibration)
    print(f"  RawData Device SN == CalData Device SN ✓")
    print(f"  PatientID non-empty ✓")

    # Compute biomarkers
    print(f"\n{'─'*65}")
    print("STEP 5 — Compute biomarkers  (2D matrix → scalar values)")
    outputs = p.compute_patient_outputs(raw_matrix, calibration, patient_ids, sensor_ids)
    unit = "mEq/L" if outputs.calibration_applied else "mV (raw)"
    print(f"  TactTime    : {outputs.tact_time} s")
    print(f"  UTemp       : {outputs.utemp} °C")
    print(f"  ETemp       : {outputs.etemp} °C")
    print(f"  DTemp3      : {outputs.dtemp3} °C")
    print(f"  UVolume     : {outputs.urine_volume} ml")
    print(f"  USodium     : {outputs.u_sodium} {unit}")
    print(f"  UPotassium  : {outputs.u_potassium} {unit}")
    print(f"  UpH         : {outputs.u_ph}")
    print(f"  UCon        : {outputs.u_con}")
    print(f"  Na/K ratio  : {outputs.na_k_ratio}")
    print(f"  Status      : {outputs.status} {'(OK)' if outputs.status == 0 else '(dry run)' if outputs.status == -1 else ''}")

    # Build 1D vector
    print(f"\n{'─'*65}")
    print("STEP 6 — Build 1D vector  (the output)")
    vector_1d = p.build_1d_vector(
        patient_output_data=p.build_patient_output_data(outputs),
        urine_volume=outputs.urine_volume,
        urine_sodium=outputs.u_sodium,
        urine_potassium=outputs.u_potassium,
        na_k_ratio=outputs.na_k_ratio,
    )
    print(f"  Length  : {len(vector_1d)} elements ✓")
    print(f"  Types   : {[type(v).__name__ for v in vector_1d]}")
    print(f"  [0] str : {vector_1d[0][:65]}...")
    print(f"  [1]     : {vector_1d[1]} ml           (urine_volume)")
    print(f"  [2]     : {vector_1d[2]} {unit}       (urine_sodium)")
    print(f"  [3]     : {vector_1d[3]} {unit}       (urine_potassium)")
    print(f"  [4]     : {vector_1d[4]}               (na_k_ratio)")

    # Build Kubyk response
    print(f"\n{'─'*65}")
    print("STEP 7 — Build Kubyk response")
    values = p.build_kubyk_values(
        raw_input_data=p.normalize_compact_string(raw_csv_text),
        calibration_input_data=p.normalize_compact_string(cal_csv_text),
        calibration_output_data=p.build_calibration_output_data(calibration),
        vector_1d=vector_1d,
        datestamp=outputs.datestamp,
        calibration_applied=outputs.calibration_applied,
        is_calibration_session=False,
        outputs=outputs,
        calibration=calibration,
    )
    response = {"sessionId": kubyk_request["sessionId"], "values": values}
    print(f"  sessionId  : {response['sessionId']}")
    print(f"  Parameters : {len(response['values'])}")
    for v in response["values"]:
        val_str  = str(v["value"])
        preview  = val_str[:55] + "..." if len(val_str) > 55 else val_str
        unit_str = f" ({v['unitMeasure']})" if v.get("unitMeasure") else ""
        print(f"    {v['parameterKey']:<28} [{type(v['value']).__name__}]{unit_str}")
        print(f"      → {preview}")

    # Save result
    import json as _json
    output_file = "result_1d_vector.json"
    with open(output_file, "w") as f:
        _json.dump({"vector_1d": vector_1d}, f, indent=2)

    unit_label = "mEq/L" if outputs.calibration_applied else "mV (raw)"
    print(f"\n{'='*65}")
    print("OUTPUT — 1D VECTOR RESULT")
    print(f"{'='*65}")
    print(f"  Device SN     : {outputs.device_id}")
    print(f"  PatientID     : {outputs.patient_id}")
    print(f"  SensorID      : {outputs.sensor_id}")
    print(f"  DateStamp     : {outputs.datestamp}")
    print(f"  TactTime      : {outputs.tact_time} s")
    print(f"  UTemp         : {outputs.utemp} °C")
    print(f"  ETemp         : {outputs.etemp} °C")
    print(f"  DTemp3        : {outputs.dtemp3} °C")
    print(f"  UVolume       : {outputs.urine_volume} ml")
    print(f"  USodium       : {outputs.u_sodium} {unit_label}")
    print(f"  UPotassium    : {outputs.u_potassium} {unit_label}")
    print(f"  UpH           : {outputs.u_ph}")
    print(f"  UCon          : {outputs.u_con}")
    print(f"  Na/K ratio    : {outputs.na_k_ratio}")
    print(f"  Status        : {outputs.status}")
    print(f"  Cal applied   : {outputs.calibration_applied}")
    print(f"{'─'*65}")
    print(f"  1D Vector (5 elements):")
    print(f"  [0] {vector_1d[0][:60]}...")
    print(f"  [1] {vector_1d[1]}  (urine_volume ml)")
    print(f"  [2] {vector_1d[2]}  (urine_sodium {unit_label})")
    print(f"  [3] {vector_1d[3]}  (urine_potassium {unit_label})")
    print(f"  [4] {vector_1d[4]}  (na_k_ratio)")
    print(f"{'─'*65}")
    print(f"  Result saved to: {output_file} ✓")
    print(f"{'='*65}")
    print("SUCCESS ✓")
    if not outputs.calibration_applied:
        print()
        print("NOTE: No CalData provided — values are raw voltages (mV).")
        print("      Run with --cal CalData.csv for clinical concentrations.")
    print(f"{'='*65}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unephra Processor Demo")
    parser.add_argument("--raw", default="RawData.csv", help="Path to RawData CSV (default: RawData.csv)")
    parser.add_argument("--cal", default=None,          help="Path to CalData CSV (optional)")
    args = parser.parse_args()
    run(args.raw, args.cal)
