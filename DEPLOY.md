# Deployment Guide — Unephra Processor v7.0.0

## Command to run

```bash
python processor1d.py
```

---

## Requirements

```bash
pip install -r requirements.txt
```

- Python 3.9+
- `numpy`
- `kbm_ledsas_sdk` — private, provided by LEDSAS team

---

## Verify before deploying

```bash
pip install numpy
python test_processor1d.py
```

Expected:
```
Results: 38 passed, 0 failed out of 38 tests
All tests passed. Code is ready for production.
```

---

## Environment

- No environment variables required
- No database or external storage required
- No config files required
- Stateless — safe to run multiple instances

---

## How it connects

Self-registers with LEDSAS Orchestrator on startup:
- **Service name:** `unephra-processor-direct`
- **Command name:** `ProcessCSV`

RabbitMQ connection settings are managed by `kbm_ledsas_sdk`.

---

## Logs

Logs go to stdout:
```
2024-11-01 10:23:45 - unephra.processor.production - INFO - Starting Unephra production processor for Kubyk version=7.0.0
```

Redirect to file if needed:
```bash
python processor1d.py >> processor.log 2>&1
```

---

## Stopping

```bash
Ctrl+C
```

Safe to stop at any time — stateless, no data is lost.

---

## Files

```
processor1d.py         — main service (run this)
test_processor1d.py    — test suite (run before deploying)
demo_processor.py      — local demo with real CSV files
RawData.csv            — sample V7 raw input data
CalData.csv            — sample calibration data
requirements.txt       — Python dependencies
README.md              — full documentation
DEPLOY.md              — this file
CHANGELOG.md           — version history
CHANGES_FOR_KUBYK.md   — summary of changes for Kubyk team
```
