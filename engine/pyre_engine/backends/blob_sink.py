"""POC record sink: append blobs you can open in the portal and read.

Production posts signals and alerts to the Cribl lake, which routes them into
datasets by `_dataset`. A blob container has no routing layer, so this sink does
that split itself - one append blob per stream, per UTC day:

    <container>/signals/2026-07-31.jsonl     every rule() match, never deduped
    <container>/alerts/2026-07-31.jsonl      every alert, deduped

Both files are newline-delimited JSON. Every record carries `p_record_type`
("signal" or "alert") and a unique id (`p_signal_id` / `p_alert_id`), so the two
streams stay identifiable even if you concatenate them.

An APPEND blob is the right primitive: appending is one server-side operation
with no read-modify-write, so concurrent workers cannot clobber each other and
nothing already written is ever rewritten.

WHY ALERTS ARE DEDUPED HERE AND SIGNALS ARE NOT
-----------------------------------------------
Signals are an audit of everything that matched - duplicates are meaningful and
must never be dropped.

Alerts are the opposite: one alert should appear once. The processor already
claims each alert atomically before dispatch, so on Redis this sink would never
see a repeat. On the POC's in-process state that claim is lost when a worker
restarts, and Event Hubs is at-least-once, so the same alert CAN arrive here
twice. The seen-set below is the compensation for that, and it exists only
because there is no shared state store yet. Switching to Redis makes it
redundant - harmless, but redundant.
"""
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone

log = logging.getLogger("pyre.sink.blob")

# Append blobs cap a single append at 4 MiB. Stay well under it so a big batch is
# split rather than rejected.
_MAX_APPEND_BYTES = 2 * 1024 * 1024
# Bound on remembered alert ids. Far above any real alert rate, and bounded so a
# long-running worker can't grow this without limit.
_SEEN_ALERTS_MAX = 10_000


class BlobSink:
    def __init__(self, account_url: str, container: str):
        self._account_url = account_url
        self._container = container
        self._svc = None
        self._container_ready = False
        self._seen_alerts: OrderedDict[str, None] = OrderedDict()

    # ---- the sink contract --------------------------------------------------
    def write(self, records: list[dict]) -> None:
        """Split by record type and append each stream. Never raises: losing the
        write must not fail the batch that produced it, because Event Hubs would
        redeliver it and the alert would fire twice. Failures surface in
        Application Insights."""
        if not records or not self._account_url:
            return
        signals = [r for r in records if r.get("p_record_type") != "alert"]
        alerts = [r for r in records if r.get("p_record_type") == "alert"]
        if alerts:
            alerts = self._new_alerts_only(alerts)
        for prefix, batch in (("signals", signals), ("alerts", alerts)):
            if batch:
                self._append(prefix, batch)

    # ---- alert dedup --------------------------------------------------------
    def _new_alerts_only(self, alerts: list[dict]) -> list[dict]:
        out = []
        for rec in alerts:
            aid = rec.get("p_alert_id")
            if aid and aid in self._seen_alerts:
                log.info("alert %s already written to blob; not appending again", aid)
                continue
            if aid:
                self._seen_alerts[aid] = None
                while len(self._seen_alerts) > _SEEN_ALERTS_MAX:
                    self._seen_alerts.popitem(last=False)
            out.append(rec)
        return out

    # ---- azure --------------------------------------------------------------
    def _service(self):
        if self._svc is None:
            # Lazy import so a local/offline run never needs the Azure SDK.
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
            # AZURE_CLIENT_ID selects the user-assigned identity when the app has
            # more than one; unset (system-assigned) is fine and ignored.
            cred = DefaultAzureCredential(
                managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID") or None
            )
            self._svc = BlobServiceClient(self._account_url, credential=cred)
        return self._svc

    def _blob_client(self, prefix: str):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        svc = self._service()
        if not self._container_ready:
            try:
                svc.create_container(self._container)
            except Exception:
                pass                    # already exists, or no create permission
            self._container_ready = True
        client = svc.get_blob_client(self._container, f"{prefix}/{day}.jsonl")
        try:
            client.create_append_blob()  # first write of the day
        except Exception:
            pass                        # already exists - the normal path
        return client

    def _append(self, prefix: str, records: list[dict]) -> None:
        try:
            client = self._blob_client(prefix)
            chunk = b""
            for rec in records:
                line = (json.dumps(rec, default=str) + "\n").encode("utf-8")
                if chunk and len(chunk) + len(line) > _MAX_APPEND_BYTES:
                    client.append_block(chunk)
                    chunk = b""
                chunk += line
            if chunk:
                client.append_block(chunk)
        except Exception:
            log.exception("append-blob write failed for %s/%s (%d record(s) dropped)",
                          self._container, prefix, len(records))
