"""Where signals and alerts go.

Three settings, checked in this order:

    OUTPUT_HTTP_URL             POST every record to an HTTP endpoint. The
                                production path: a SIEM/lake HTTP source.
    OUTPUT_BLOB_ACCOUNT_URL     append every record to a blob you can open and
                                read in the portal. The POC/dev path.
    (neither)                   detection still runs, records go nowhere, and a
                                warning says so.

Plus one optional extra, independent of the above:

    ALERT_WEBHOOK_URL           POST each ALERT to a case tool as well.

A sink NEVER raises. Losing a write must not fail the batch: Event Hubs would
redeliver it and the alert would fire twice. Failures land in Application
Insights instead.
"""
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone

import requests

log = logging.getLogger("pyre.sink")

# A single append-blob operation caps at 4 MiB. Stay well under so a big batch is
# split rather than rejected.
_MAX_APPEND_BYTES = 2 * 1024 * 1024
# Bound on remembered alert ids - far above any real alert rate, but bounded so a
# long-running worker can't grow it without limit.
_SEEN_ALERTS_MAX = 10_000


def build_sink(cfg):
    """Pick the sink from config, and wrap it in the alert webhook if one is
    configured. The engine above this never learns which it got."""
    if cfg.output_http_url:
        log.info("output: http -> %s", cfg.output_http_url)
        sink = HttpSink(cfg.output_http_url)
    elif cfg.output_blob_account_url:
        log.info("output: blob -> %s/%s", cfg.output_blob_account_url, cfg.output_blob_container)
        sink = BlobSink(cfg.output_blob_account_url, cfg.output_blob_container)
    else:
        log.warning("no output configured (set OUTPUT_BLOB_ACCOUNT_URL or OUTPUT_HTTP_URL); "
                    "signals and alerts will be dropped")
        sink = _NullSink()
    if cfg.alert_webhook_url:
        log.info("alerts also POSTed to %s", cfg.alert_webhook_url)
        sink = AlertWebhook(sink, cfg.alert_webhook_url)
    return sink


class _NullSink:
    def write(self, records: list[dict]) -> None:
        pass


class HttpSink:
    """POST the batch as a JSON array. Both streams travel together; the far end
    splits them on `p_record_type`."""

    def __init__(self, url: str, timeout: int = 10):
        self._url = url
        self._timeout = timeout

    def write(self, records: list[dict]) -> None:
        if not records or not self._url:
            return
        try:
            requests.post(self._url, json=records, timeout=self._timeout)
        except Exception:
            log.exception("output POST failed (%d record(s) dropped)", len(records))


class AlertWebhook:
    """Decorator: passes everything through to the real sink, and additionally
    POSTs each alert on its own to a case tool. Alerts are recorded either way -
    a webhook that is down loses the page, never the record."""

    def __init__(self, inner, url: str, timeout: int = 10):
        self._inner = inner
        self._url = url
        self._timeout = timeout

    def write(self, records: list[dict]) -> None:
        self._inner.write(records)
        for rec in records:
            if rec.get("p_record_type") != "alert":
                continue
            try:
                requests.post(self._url, json=rec, timeout=self._timeout)
            except Exception:
                log.exception("alert webhook failed for %s", rec.get("p_alert_id"))


class BlobSink:
    """One append blob per stream, per UTC day:

        <container>/signals/2026-07-31.jsonl
        <container>/alerts/2026-07-31.jsonl

    Newline-delimited JSON, readable straight from the portal's storage browser.

    An APPEND blob is the right primitive here: appending is one server-side
    operation with no read-modify-write, so concurrent workers cannot clobber
    each other and nothing already written is ever rewritten.

    Alerts get an extra dedup pass that signals deliberately do not. Signals are
    an audit trail, so repeats there are meaningful. An alert should appear once,
    and while the processor already claims each alert atomically, that claim is
    lost when a worker restarts on in-process state - so with Event Hubs'
    at-least-once delivery the same alert can reach this sink twice. On Redis
    this pass is redundant, and harmless.
    """

    def __init__(self, account_url: str, container: str):
        self._account_url = account_url
        self._container = container
        self._svc = None
        self._container_ready = False
        self._seen_alerts: OrderedDict[str, None] = OrderedDict()

    def write(self, records: list[dict]) -> None:
        if not records or not self._account_url:
            return
        signals = [r for r in records if r.get("p_record_type") != "alert"]
        alerts = self._new_alerts_only([r for r in records if r.get("p_record_type") == "alert"])
        for prefix, batch in (("signals", signals), ("alerts", alerts)):
            if batch:
                self._append(prefix, batch)

    def _new_alerts_only(self, alerts: list[dict]) -> list[dict]:
        out = []
        for rec in alerts:
            aid = rec.get("p_alert_id")
            if aid and aid in self._seen_alerts:
                log.info("alert %s already written; not appending again", aid)
                continue
            if aid:
                self._seen_alerts[aid] = None
                while len(self._seen_alerts) > _SEEN_ALERTS_MAX:
                    self._seen_alerts.popitem(last=False)
            out.append(rec)
        return out

    def _service(self):
        if self._svc is None:
            # Imported lazily so local/offline runs never need the Azure SDK.
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
            cred = DefaultAzureCredential(
                managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID") or None)
            self._svc = BlobServiceClient(self._account_url, credential=cred)
        return self._svc

    def _blob_client(self, prefix: str):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        svc = self._service()
        if not self._container_ready:
            try:
                svc.create_container(self._container)
            except Exception:
                pass                    # already exists, or no permission to create
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
