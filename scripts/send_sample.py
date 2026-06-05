#!/usr/bin/env python3
"""End-to-end sender for the LEDSAS heart-rate fitness-age demo.

This script is the *caller* side of the demo: it uploads a sample CSV to
blob storage, publishes a LEDSAS command envelope to the service's command
exchange, waits for the response on a transient reply exchange it owns, and
prints the output blob URI + computed fitness age.

It deliberately uses **azure-storage-blob + aio-pika directly** -- NOT the
LEDSAS SDK. The SDK is the service/handler side; a caller (orchestrator,
test rig, this script) just speaks AMQP + Blob. Both libraries are already
present as transitive dependencies of the SDK, so no extra install is needed
in the demo image / dev venv.

Reply-to contract (per QUICKSTART_DIRECT_MODE.md §3.5): the caller owns the
reply topology. This script declares a transient reply exchange + queue +
binding (routing key ``response``), sets ``envelope.reply_to`` to that
exchange name, and the SDK publishes the handler's return value there. We
match the response on ``correlation_id`` and clean up the reply exchange on
exit.

Idempotency (per the TASK-028 design): every invocation uses
``idempotency_key=f"demo-{sample_stem}-{utc_iso8601_second}"`` so the
``overwrite=True`` handler can be re-run safely.

Usage::

    python scripts/send_sample.py samples/young_athlete.csv
    python scripts/send_sample.py samples/older_sedentary.csv --timeout 30

Environment (read from process env; see .env.example):
    KBM_LEDSAS_SERVICE_NAME   (default HeartRateFitnessAge)
    KBM_LEDSAS_TENANT         (default demo)
    KBM_LEDSAS_RABBITMQ_URL   (default amqp://guest:guest@127.0.0.1:5672/)
    KBM_LEDSAS_BLOB_CONN_STRING (Azurite default if unset)
    KBM_LEDSAS_CONTAINER      (default heart-rate-fitness-age)

Exit codes: 0 = response received; 1 = timeout / error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Pure, import-and-test-without-a-broker helpers.
# ---------------------------------------------------------------------------

DEFAULT_SERVICE_NAME = "HeartRateFitnessAge"
DEFAULT_TENANT = "demo"
DEFAULT_RABBITMQ_URL = "amqp://guest:guest@127.0.0.1:5672/"
DEFAULT_BLOB_CONN_STRING = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)
DEFAULT_CONTAINER = "heart-rate-fitness-age"

# Per-sample demographics so age/sex match the synthetic recordings.
# Keys are sample *filenames* (basename). These mirror samples/generate.py's
# Scenario catalogue; kept here so the sender is self-contained and does not
# import the generator (the generator may not ship in every runtime image).
SAMPLE_DEMOGRAPHICS: Dict[str, Dict[str, Any]] = {
    "young_athlete.csv": {"age": 28, "sex": "male", "weight_kg": 72, "height_cm": 180},
    "midlife_active.csv": {"age": 42, "sex": "female", "weight_kg": 64, "height_cm": 166},
    "midlife_sedentary.csv": {"age": 45, "sex": "male", "weight_kg": 92, "height_cm": 176},
    "older_active.csv": {"age": 65, "sex": "female", "weight_kg": 66, "height_cm": 162},
    "older_sedentary.csv": {"age": 68, "sex": "male", "weight_kg": 88, "height_cm": 174},
}


def demographics_for(sample_path: str) -> Dict[str, Any]:
    """Return demographics for a sample CSV (matched on basename).

    Raises:
        KeyError: when the sample filename has no registered demographics.
    """
    stem = Path(sample_path).name
    try:
        return dict(SAMPLE_DEMOGRAPHICS[stem])
    except KeyError as e:
        raise KeyError(
            f"no demographics registered for {stem!r}; "
            f"known: {sorted(SAMPLE_DEMOGRAPHICS)}"
        ) from e


def build_idempotency_key(sample_path: str, now: Optional[datetime] = None) -> str:
    """Build the demo idempotency key: demo-{stem}-{utc-iso8601-second}.

    The CSV extension is dropped from the stem; the timestamp is truncated to
    whole seconds so re-sends within the same second collapse to one logical
    request (matching the ``overwrite=True`` handler).
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    stem = Path(sample_path).stem
    return f"demo-{stem}-{stamp}"


def build_command_envelope(
    *,
    handler_name: str,
    reply_to: str,
    idempotency_key: str,
    correlation_id: Optional[str] = None,
    message_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    sent_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build a LEDSAS command envelope (the routing/metadata block).

    Matches the format documented in QUICKSTART_DIRECT_MODE.md §3.4/§3.5.
    """
    sent_at = sent_at or datetime.now(timezone.utc)
    return {
        "schema_version": "1.0",
        "type": "command",
        "name": handler_name,
        "message_version": "1.0",
        "message_id": message_id or str(uuid.uuid4()),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "idempotency_key": idempotency_key,
        "sent_at": sent_at.isoformat(),
        "trace_id": trace_id or str(uuid.uuid4()),
        "reply_to": reply_to,
    }


def build_command_message(
    *,
    handler_name: str,
    reply_to: str,
    idempotency_key: str,
    input_blob: str,
    demographics: Dict[str, Any],
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the full {envelope, payload} command message for the HR handler."""
    return {
        "envelope": build_command_envelope(
            handler_name=handler_name,
            reply_to=reply_to,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        ),
        "payload": {
            "demographics": demographics,
            "input_blob": input_blob,
        },
    }


def build_command_exchange_name(tenant: Optional[str], service_name: str) -> str:
    """Reproduce the SDK's command-exchange naming.

    Mirrors ``kbm_ledsas_sdk.amqp.topology.build_exchange_name("cmd", ...)``:
      - with tenant:    cmd.{tenant}.{service}.v1
      - without tenant: cmd.{service}.v1
    """
    if tenant:
        return f"cmd.{tenant}.{service_name}.v1"
    return f"cmd.{service_name}.v1"


def build_input_blob_uri(container: str, idempotency_key: str) -> str:
    """Deterministic input blob URI for an uploaded sample CSV."""
    return f"azblob://{container}/samples/{idempotency_key}.csv"


def parse_blob_uri(uri: str) -> tuple[str, str]:
    """Split an ``azblob://<container>/<path>`` URI into (container, blob).

    Raises:
        ValueError: when the URI is not an azblob:// URI.
    """
    prefix = "azblob://"
    if not uri.startswith(prefix):
        raise ValueError(f"not an azblob:// URI: {uri!r}")
    rest = uri[len(prefix):]
    container, _, blob = rest.partition("/")
    if not container or not blob:
        raise ValueError(f"malformed azblob URI: {uri!r}")
    return container, blob


class Env:
    """Resolved environment configuration (with documented defaults)."""

    def __init__(self, environ: Optional[Dict[str, str]] = None) -> None:
        e = environ if environ is not None else os.environ
        self.service_name = e.get("KBM_LEDSAS_SERVICE_NAME", DEFAULT_SERVICE_NAME)
        self.tenant = e.get("KBM_LEDSAS_TENANT", DEFAULT_TENANT) or None
        self.rabbitmq_url = e.get("KBM_LEDSAS_RABBITMQ_URL", DEFAULT_RABBITMQ_URL)
        self.blob_conn_string = e.get(
            "KBM_LEDSAS_BLOB_CONN_STRING", DEFAULT_BLOB_CONN_STRING
        )
        self.container = e.get("KBM_LEDSAS_CONTAINER", DEFAULT_CONTAINER)

    @property
    def command_exchange(self) -> str:
        return build_command_exchange_name(self.tenant, self.service_name)


# ---------------------------------------------------------------------------
# Live send/receive (imports the heavy libs lazily so the helpers above can be
# imported + unit-tested without azure / aio_pika installed).
# ---------------------------------------------------------------------------


def _upload_sample(env: Env, local_path: Path, blob_name: str) -> str:
    """Upload the local CSV to blob storage; return the azblob:// URI."""
    from azure.storage.blob import BlobServiceClient

    svc = BlobServiceClient.from_connection_string(env.blob_conn_string)
    try:
        try:
            svc.create_container(env.container)
        except Exception:  # noqa: BLE001 — container may already exist
            pass
        blob = svc.get_blob_client(container=env.container, blob=blob_name)
        blob.upload_blob(local_path.read_bytes(), overwrite=True)
    finally:
        svc.close()
    return f"azblob://{env.container}/{blob_name}"


async def _publish_and_wait(
    env: Env,
    *,
    message: Dict[str, Any],
    reply_exchange: str,
    reply_queue: str,
    correlation_id: str,
    timeout: float,
) -> Dict[str, Any]:
    """Declare reply topology, publish the command, await the matching reply."""
    import aio_pika

    connection = await aio_pika.connect_robust(env.rabbitmq_url)
    try:
        channel = await connection.channel()

        # Caller-owned reply topology (the SDK does NOT declare this).
        rx = await channel.declare_exchange(
            reply_exchange, aio_pika.ExchangeType.TOPIC, durable=True
        )
        rq = await channel.declare_queue(
            reply_queue, durable=False, auto_delete=True
        )
        await rq.bind(rx, routing_key="response")

        # Reference the command exchange the service declares (do NOT redeclare
        # it with conflicting args — passive lookup).
        cmd_ex = await channel.get_exchange(env.command_exchange, ensure=True)
        await cmd_ex.publish(
            aio_pika.Message(
                body=json.dumps(message).encode("utf-8"),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key="command",
        )

        # Await the matching response.
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def on_message(msg: "aio_pika.abc.AbstractIncomingMessage") -> None:
            async with msg.process():
                try:
                    body = json.loads(msg.body)
                except Exception:  # noqa: BLE001
                    return
                env_block = body.get("envelope", {})
                if env_block.get("correlation_id") == correlation_id:
                    if not future.done():
                        future.set_result(body)

        consumer_tag = await rq.consume(on_message)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            await rq.cancel(consumer_tag)
            # Demo cleanup: remove the transient reply exchange we created.
            try:
                await rx.delete(if_unused=True)
            except Exception:  # noqa: BLE001
                pass
    finally:
        await connection.close()


async def _run(args: argparse.Namespace) -> int:
    env = Env()
    local_path = Path(args.sample)
    if not local_path.is_file():
        print(f"error: sample not found: {local_path}", file=sys.stderr)
        return 1

    try:
        demographics = demographics_for(str(local_path))
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    idempotency_key = build_idempotency_key(str(local_path))
    blob_name = f"samples/{idempotency_key}.csv"
    correlation_id = str(uuid.uuid4())
    reply_exchange = f"reply-ex-hr-{uuid.uuid4().hex[:8]}"
    reply_queue = f"{reply_exchange}-q"

    print(f"Service:        {env.service_name} (tenant={env.tenant})")
    print(f"Cmd exchange:   {env.command_exchange}")
    print(f"Sample:         {local_path}  demographics={demographics}")
    print(f"Idempotency:    {idempotency_key}")

    # 1. Upload the sample CSV.
    try:
        input_blob = _upload_sample(env, local_path, blob_name)
    except Exception as e:  # noqa: BLE001
        print(f"error: blob upload failed: {e}", file=sys.stderr)
        return 1
    print(f"Uploaded:       {input_blob}")

    # 2. Build the command message.
    message = build_command_message(
        handler_name="fitness_age",
        reply_to=reply_exchange,
        idempotency_key=idempotency_key,
        input_blob=input_blob,
        demographics=demographics,
        correlation_id=correlation_id,
    )

    # 3. Publish + wait for the reply.
    print(f"Publishing command, waiting <= {args.timeout:.0f}s for response ...")
    try:
        response = await _publish_and_wait(
            env,
            message=message,
            reply_exchange=reply_exchange,
            reply_queue=reply_queue,
            correlation_id=correlation_id,
            timeout=args.timeout,
        )
    except asyncio.TimeoutError:
        print(
            f"error: no response within {args.timeout:.0f}s "
            "(is the service running and consuming?)",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"error: publish/receive failed: {e}", file=sys.stderr)
        return 1

    payload = response.get("payload", {})
    print("\n=== Response ===")
    print(f"status:            {payload.get('status')}")
    print(f"output_blob:       {payload.get('output_blob')}")
    print(f"fitness_age:       {payload.get('fitness_age')}")
    print(f"vo2_max_estimated: {payload.get('vo2_max_estimated')}")
    return 0 if payload.get("status") == "ok" else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload a HR sample, publish the fitness_age command, "
        "and print the response."
    )
    parser.add_argument("sample", help="Path to a sample CSV (e.g. samples/young_athlete.csv)")
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Seconds to wait for the response (default 30)."
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
