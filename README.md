# Heart-Rate Fitness-Age Service

A LEDSAS demo service that estimates a user's "fitness age" from a CSV
of heart-rate samples plus basic demographics. The service downloads a
CSV blob, computes resting HR / VO2max / fitness-age, writes the full
result to an output blob, and returns a compact response payload.

This repo is a complete Kubyk-pushable template: the three files
required by the Kubyk contract (`version.yaml`, `Dockerfile`, `main.py`)
plus tests and config.

## Input / Output schema

**Request (`fitness_age` command payload):**

```json
{
  "demographics": {
    "age": 42,
    "sex": "male",
    "weight_kg": 80,
    "height_cm": 178
  },
  "input_blob": "azblob://heart-rate-fitness-age/samples/run-001.csv"
}
```

The blob at `input_blob` must be a CSV with two columns:

```
timestamp_iso,heart_rate_bpm
2026-05-28T09:00:00Z,62
2026-05-28T09:00:01Z,63
...
```

**Response:**

```json
{
  "status": "ok",
  "output_blob": "azblob://heart-rate-fitness-age/fitness-age/<idempotency_key>.json",
  "fitness_age": 38,
  "vo2_max_estimated": 42.7
}
```

The output blob contains the full breakdown (`resting_hr_bpm`,
`avg_hr_bpm`, `max_hr_predicted`, `vo2_max_estimated`, `fitness_age`,
and an echo of the request).

## What this demo does NOT do

The fitness-age formula here is a simplified public-knowledge
approximation derived from the Nes/Wisløff Norwegian cohort (linear
inverse-fit on VO2max). It is **deliberately conservative** and meant
only for demoing the LEDSAS pipeline shape. In particular:

- VO2max is estimated from `max_hr_predicted / resting_hr_bpm * 15.3`
  — a rough published correlation, not a calibrated model.
- No clinical validation, no input-quality checks beyond "non-empty
  numeric column", no handling of artefacts, no per-individual
  calibration.
- Production deployments must replace `compute_fitness_age()` with a
  validated model and run it through your regulatory process.

Treat the numbers this service returns as illustrative.

## Sample data (`samples/`)

The `samples/` directory ships **five purely synthetic** 24-hour heart-rate
recordings (1440 rows each, 1-minute sampling) spanning a fitness gradient
from young athlete to older sedentary. No real subject data is used.

| Sample | Demographics | Expected fitness-age direction |
|--------|--------------|-------------------------------|
| `young_athlete.csv` | 28 M | **youngest** — well below chronological age |
| `midlife_active.csv` | 42 F | younger than chronological age |
| `midlife_sedentary.csv` | 45 M | older than `midlife_active` |
| `older_active.csv` | 65 F | younger than `older_sedentary` |
| `older_sedentary.csv` | 68 M | **oldest** — well above chronological age |

Each recording is a transparent composition of: a per-scenario resting
baseline, a non-negative circadian elevation (HR rises while awake, settles
back to resting overnight), Gaussian activity bouts (a morning + evening
workout for the active scenarios; minimal for the sedentary ones), and an
AR(1) heart-rate-variability noise term. The age-predicted maximum uses the
**Tanaka 2001** formula `HRmax = 208 − 0.7·age`. Parameter choices are tuned
so the demo's **Nes/Wisløff (HUNT cohort)** inverse-fit fitness-age estimate
lands in a plausible, scenario-appropriate range.

**Source papers (cited, not bundled):**

- Tanaka H, Monahan KD, Seals DR. *Age-predicted maximal heart rate
  revisited.* J Am Coll Cardiol. 2001;37(1):153-156.
- Nes BM, Janszky I, Wisløff U, et al. *Age-predicted maximal heart rate in
  healthy subjects: The HUNT fitness study.* Scand J Med Sci Sports.
  2013;23(6):697-704.

### Calibration note (synthetic-data ⇄ demo formula)

The demo handler's `compute_fitness_age` recovers "resting HR" as the 5th
percentile of the **whole-day** distribution and feeds it through
`vo2 = HRmax/resting·15.3` plus a linear inverse fit. The meaningful
fitness-age band `[20, 90]` for that formula corresponds to a 5th-percentile
HR of roughly 70–130 bpm. The generator therefore applies a small per-scenario
`resting_offset_bpm` calibration shift so the *handler's* output spreads
visibly across `[20, 90]` rather than collapsing every realistic resting rate
to the clamp floor. This shift is documented in `samples/generate.py`; the
circadian amplitude, activity bouts, and AR(1) noise parameters are unchanged.
For a production deployment you would replace the formula with a validated
model and feed it real recordings — no calibration shim required.

### Regenerating the samples

The samples are committed artifacts, but you can re-create them (or generate
your own scenario set) deterministically:

```bash
# regenerate all 5 committed samples (byte-identical to what ships)
python samples/generate.py

# list the scenarios + demographics
python samples/generate.py --list

# alternate seed for a fresh-but-statistically-equivalent sample set
python samples/generate.py --seed 1234 --out /tmp/my-samples
```

Same seed ⇒ byte-identical CSVs (the per-scenario seed is derived from the
scenario name via BLAKE2b, so it is stable across machines and Python runs).

## End-to-end: pull → deploy on Kubyk → send a sample → observe

This is the full customer flow, from extracting the SDK tarball to seeing a
fitness-age result come back.

### 1. Pull this template out of the SDK tarball

```bash
tar -xzf ledsas-sdk-direct-v0.2.2.tar.gz
cd ledsas-sdk-direct-v0.2.2/5-templates/heart_rate_fitness_age/
```

(Before v0.2.2 the demos lived only in the SDK source repo. From v0.2.2 both
demos ship under `5-templates/` inside the direct tarball.)

### 2. Push it to a fresh git repo Kubyk watches

```bash
git init && git add -A && git commit -m "init heart-rate fitness-age service"
git remote add kubyk <your-kubyk-repo-url>
git push kubyk main
```

Kubyk picks up the repo via the standard contract (it reads `version.yaml`
for the tag prefix and builds the `Dockerfile`). See
`PATH_TO_PRODUCTION.md` in the tarball root for the customer-facing
walkthrough; ask your KeborMed contact for the full Kubyk CI/CD contract
document.

### 3. Set the broker + blob env vars

On Kubyk these come from your platform's secrets management. **Locally** you
set them in `.env` (copy `.env.example`). The wire protocols (AMQP TLS,
Azure Blob HTTPS) are identical between local and Kubyk — only the source of
the credentials differs.

```bash
cp .env.example .env
# edit .env: point KBM_LEDSAS_RABBITMQ_URL + KBM_LEDSAS_BLOB_CONN_STRING at
# your broker + storage account (defaults target the bundled local stack).
```

### 4. Run the service

On Kubyk this is automatic once the image is deployed. Locally:

```bash
docker build --build-arg SDK_VERSION=0.2.2 -t heart-rate-fitness-age:local .
docker run --env-file .env --network host heart-rate-fitness-age:local
# or, in a venv with the SDK wheel installed:  python main.py
```

> **Container networking:** see the SDK release's
> `3-local-development/README.md` for the container-to-host networking notes
> and the `KBM_LEDSAS_ALLOW_INSECURE_AMQP=1` requirement when pointing at a
> local plaintext broker. Note `--network host` is **Linux-only** — Docker
> Desktop for macOS does not share the host loopback, so use
> `host.docker.internal` in your `.env` endpoints there instead.

The service connects to RabbitMQ, declares its command topology
(`cmd.demo.HeartRateFitnessAge.v1`), and starts consuming.

### 5. Send a sample and observe the result

`scripts/send_sample.py` is the **caller** side. It uploads a sample CSV to
blob storage, publishes the `fitness_age` command, waits up to 30 s on a
transient reply exchange it owns, and prints the result. It speaks
azure-storage-blob + aio-pika **directly** (not via the SDK — the SDK is the
service side).

```bash
# reads the same env vars as the service (.env / Kubyk secrets)
python scripts/send_sample.py samples/young_athlete.csv
```

Expected output (values illustrative):

```
Service:        HeartRateFitnessAge (tenant=demo)
Cmd exchange:   cmd.demo.HeartRateFitnessAge.v1
Sample:         samples/young_athlete.csv  demographics={'age': 28, 'sex': 'male', ...}
Idempotency:    demo-young_athlete-20260528T101500Z
Uploaded:       azblob://heart-rate-fitness-age/samples/demo-young_athlete-...csv
Publishing command, waiting <= 30s for response ...

=== Response ===
status:            ok
output_blob:       azblob://heart-rate-fitness-age/fitness-age/demo-young_athlete-...json
fitness_age:       26
vo2_max_estimated: 40.0
```

You then **observe the result two ways**:

- **In Kubyk:** the service's structured logs show the command being
  consumed + the output blob being written (filter on your service name).
- **In Azure (or Azurite locally):** the output blob lands under
  `fitness-age/<idempotency_key>.json` in the configured container and holds
  the full breakdown (resting HR, average HR, predicted HRmax, VO2max,
  fitness age, and an echo of the request).

Re-running `send_sample.py` is safe: each invocation derives an idempotency
key from the sample name + UTC second, and the handler uploads with
`overwrite=True`, so a replay rewrites the same output blob rather than
failing.

## Quick start

1. Drop the SDK tarball next to the `Dockerfile` (matching name:
   `ledsas-sdk-direct-v${SDK_VERSION}.tar.gz`).
2. Copy `.env.example` to `.env` and fill in your RabbitMQ + blob
   credentials.
3. Build and push:

```bash
docker build --build-arg SDK_VERSION=0.2.2 -t heart-rate-fitness-age:0.1.0 .
# then push to Kubyk per your normal contract:
#   git push kubyk main
```

The Kubyk contract and SDK API reference are bundled inside the LEDSAS
SDK direct-mode tarball after extraction:

- Path to production: `ledsas-sdk-direct-v0.2.2/PATH_TO_PRODUCTION.md`
- SDK API reference: `ledsas-sdk-direct-v0.2.2/2-documentation/SDK_API_REFERENCE.md`
- Changelog: `ledsas-sdk-direct-v0.2.2/CHANGELOG.md`

(If you don't have the SDK tarball yet, ask your KeborMed contact for
the latest distribution.)

## Running the tests

From this demo's directory, in your own Python 3.11+ venv:

```bash
python -m venv venv
source venv/bin/activate
pip install pytest numpy pandas
# install the bundled SDK wheel (path depends on where you placed the tarball)
pip install path/to/ledsas-sdk-direct-v0.2.2/1-sdk/kbm_ledsas_sdk-0.2.2-py3-none-any.whl
python -m pytest tests/ -q
```

The tests cover the pure `compute_fitness_age()` and `parse_hr_csv()`
helpers plus the async handler with mocked blob ops — no broker, no
Azurite required.

## Files

| File | Purpose |
|------|---------|
| `version.yaml` | Kubyk version prefix |
| `Dockerfile` | Container build (Python 3.11-slim + SDK wheel) |
| `main.py` | `ServiceApp` + `fitness_age` handler |
| `requirements.txt` | Extra deps (numpy, pandas) — SDK comes from the wheel |
| `.env.example` | Env vars the service expects |
| `tests/test_handler.py` | Unit tests (helpers, handler, samples, sender) |
| `samples/generate.py` | Deterministic synthetic-sample generator |
| `samples/*.csv` | 5 committed synthetic 24-hour HR recordings |
| `scripts/send_sample.py` | Caller: upload sample → publish command → print result |
