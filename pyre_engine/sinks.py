"""Where signals and alerts go.

Signals and alerts are INDEPENDENT streams with independent settings, because
they are consumed by different things: signals are the high-volume audit trail a
lake or a blob stores, alerts are the low-volume page a case tool acts on.

    SIGNAL_DESTINATION   blob | http | none
    ALERT_DESTINATION    blob | http | none

Each names its own account/URL. Pointing both at the same place is normal and
costs nothing extra - identical targets resolve to ONE sink instance and one
write per batch. Moving alerts to an external SIEM while signals stay in a blob
is two settings, not a code change. See docs/configuring-destinations.md.

A sink NEVER raises. Losing a write must not fail the batch: Event Hubs would
redeliver it and the alert would fire twice. Failures land in Application
Insights instead, with a count of what was dropped.

Adding a third kind of destination is one class implementing `write()` plus one
branch in `_build_sink`. Nothing above this module changes.
"""
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

log = logging.getLogger("pyre.sink")

# A single append-blob operation caps at 4 MiB. Stay well under so a big batch is
# split rather than rejected.
_MAX_APPEND_BYTES = 2 * 1024 * 1024
# Bound on remembered alert ids - far above any real alert rate, but bounded so a
# long-running worker can't grow it without limit.
_SEEN_ALERTS_MAX = 10_000

SIGNAL = "signal"
ALERT = "alert"


class Sink(Protocol):
    """The whole contract. `records` is a batch's worth of signal and/or alert
    records; the implementation must not raise."""

    def write(self, records: list[dict]) -> None: ...

    def describe(self) -> str:
        """One redacted line naming where this writes. /health reports it."""
        ...


def redact(url: str) -> str:
    """A destination URL, safe to log. Query strings on webhook URLs routinely
    carry a shared-access token (Teams, Logic Apps, Torq), and this module's
    startup lines and /health both echo the configured destination - so scheme,
    host and path only, never the credential."""
    if not url:
        return ""
    parts = urlsplit(url)
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return clean + "?..." if parts.query else clean


class Router:
    """Sends each record to its own stream's destination.

    Holds at most one sink per distinct target, so two streams pointed at the
    same place share an instance - which is what keeps a shared blob container
    or a shared HTTP endpoint to one write per batch, exactly as if there were
    one destination setting.
    """

    def __init__(self, by_stream: dict[str, Sink]):
        self._by_stream = by_stream

    def describe(self) -> dict[str, str | None]:
        """What each stream resolved to, redacted. /health reports this: the
        settings say what was ASKED for, this says what is actually wired."""
        return {stream: (sink.describe() if sink is not None else None)
                for stream, sink in self._by_stream.items()}

    def write(self, records: list[dict]) -> None:
        if not records:
            return
        # Group by destination INSTANCE, not by stream, so a shared target still
        # receives one call carrying both streams - the shape BlobSink and a
        # batching HttpSink already expect.
        batches: list[tuple[Sink, list[dict]]] = []
        for rec in records:
            stream = ALERT if rec.get("p_record_type") == ALERT else SIGNAL
            sink = self._by_stream.get(stream)
            if sink is None:
                continue
            for existing, batch in batches:
                if existing is sink:
                    batch.append(rec)
                    break
            else:
                batches.append((sink, [rec]))
        for sink, batch in batches:
            sink.write(batch)


def build_router(cfg) -> Router:
    """Resolve both streams' destinations from config. Identical targets are
    deduped to one instance."""
    built: dict[str, Sink] = {}                # target key -> sink
    by_stream: dict[str, Sink | None] = {}
    for stream in (SIGNAL, ALERT):
        kind = getattr(cfg, f"{stream}_destination")
        key, make = _target(cfg, stream)
        if key is None:
            if kind == "none":
                log.info("%s destination: none (records are discarded)", stream)
            else:
                # Selected but not configured. problems() names the exact setting;
                # this says out loud that records are being lost right now.
                log.error("%s destination is %r but not configured; %ss will be "
                          "dropped. See /health -> problems.", stream, kind, stream)
            by_stream[stream] = None
            continue
        if key not in built:
            built[key] = make()
        by_stream[stream] = built[key]
        log.info("%s destination: %s", stream, built[key].describe())
    return Router(by_stream)


def _target(cfg, stream: str):
    """(dedup key, factory) for one stream, or (None, None) for `none`.

    The key is what makes two streams pointed at the same account and container
    - or the same URL - share a single sink.
    """
    kind = getattr(cfg, f"{stream}_destination")
    if kind == "blob":
        account = getattr(cfg, f"{stream}_blob_account_url")
        container = getattr(cfg, f"{stream}_blob_container")
        if not account:
            return None, None               # reported by RuntimeConfig.problems()
        return ("blob", account, container), lambda: BlobSink(
            account, container, rollover_minutes=cfg.blob_rollover_minutes)
    if kind == "http":
        url = getattr(cfg, f"{stream}_http_url")
        header = getattr(cfg, f"{stream}_http_auth_header")
        batch = getattr(cfg, f"{stream}_http_batch")
        if not url:
            return None, None
        return ("http", url, header, batch), lambda: HttpSink(
            url, auth_header=header, batch=batch, timeout=cfg.http_timeout_seconds)
    return None, None


class HttpSink:
    """POST records to an endpoint.

    `batch` picks the shape the far end wants: True sends the whole batch as one
    JSON array (a lake or a SIEM HTTP source ingesting signals), False sends one
    record per request (a case tool that opens a ticket per alert).

    The response status IS checked. A 401 or a 500 from the receiver is a lost
    record, and treating it as success is how a destination silently stops
    working for a week.
    """

    def __init__(self, url: str, auth_header: str = "", batch: bool = True, timeout: int = 10):
        self._url = url
        self._batch = batch
        self._timeout = timeout
        # Supplied whole ("Bearer abc", "SharedKey xyz") so any scheme works
        # without a setting per scheme. Set it to a Key Vault reference.
        self._headers = {"Authorization": auth_header} if auth_header else {}

    def describe(self) -> str:
        return f"http {redact(self._url)}" + ("" if self._batch else " (one record per request)")

    def write(self, records: list[dict]) -> None:
        if not records:
            return
        if self._batch:
            self._post(records, len(records))
            return
        for rec in records:
            self._post(rec, 1)

    def _post(self, payload, count: int) -> None:
        try:
            resp = requests.post(self._url, json=payload, headers=self._headers,
                                 timeout=self._timeout)
            if resp.status_code >= 400:
                log.error("output POST to %s returned %d (%d record(s) dropped): %.200s",
                          redact(self._url), resp.status_code, count, resp.text)
        except Exception:
            log.exception("output POST to %s failed (%d record(s) dropped)",
                          redact(self._url), count)


class BlobSink:
    """One append blob per stream, bucketed by UTC time:

        <container>/signals/2026-07-31T14-00.jsonl
        <container>/alerts/2026-07-31T14-00.jsonl

    Newline-delimited JSON, readable straight from the portal's storage browser.
    The stream prefix lives INSIDE the container, so pointing both streams at one
    container keeps them separately readable, and giving each its own container
    also works - with no extra setting either way.

    An APPEND blob is the right primitive here: appending is one server-side
    operation with no read-modify-write, so concurrent workers cannot clobber
    each other and nothing already written is ever rewritten.

    The bucket is `rollover_minutes` wide (BLOB_ROLLOVER_MINUTES, default 15)
    rather than one blob per day. An append blob accepts at most 50,000 append
    operations, ever - past that, every further write to it fails. `_append()`
    does roughly one append call per batch that has records for this stream, so
    at even a modest sustained rate a single ALL-DAY blob runs out of budget
    hours before the day is over: at just 1 append/sec, 50,000 calls is only
    ~13.9 hours. Every worker across every partition writes to the SAME blob for
    a given bucket, so this only gets worse as the app scales out to more
    partitions - which is exactly the situation high volume puts it in. Bucketing
    by time gives each blob its own fresh 50,000-call budget; pick a width where
    the worst-case call rate for this instance times the bucket width stays
    comfortably under that.

    Alerts get an extra dedup pass that signals deliberately do not. Signals are
    an audit trail, so repeats there are meaningful. An alert should appear once,
    and while the processor already claims each alert atomically, that claim is
    lost when a worker restarts on in-process state - so with Event Hubs'
    at-least-once delivery the same alert can reach this sink twice. On Redis
    this pass is redundant, and harmless.
    """

    def __init__(self, account_url: str, container: str, rollover_minutes: int = 15):
        self._account_url = account_url
        self._container = container
        # Zero or negative would divide-by-zero in _bucket(); a sink must never
        # raise, so a bad setting degrades to "one blob per hour" rather than
        # crashing the batch. problems() is what actually surfaces the bad value.
        self._rollover_minutes = rollover_minutes if rollover_minutes > 0 else 60
        self._svc = None
        self._container_ready = False
        self._known_blobs: set[str] = set()  # "<prefix>/<bucket>" confirmed to exist
        self._seen_alerts: OrderedDict[str, None] = OrderedDict()

    def describe(self) -> str:
        return f"blob {redact(self._account_url)}/{self._container}"

    def write(self, records: list[dict]) -> None:
        if not records or not self._account_url:
            return
        signals = [r for r in records if r.get("p_record_type") != ALERT]
        alerts = self._new_alerts_only([r for r in records if r.get("p_record_type") == ALERT])
        for prefix, batch in (("signals", signals), ("alerts", alerts)):
            if batch:
                self._append(prefix, batch)

    def _new_alerts_only(self, alerts: list[dict]) -> list[dict]:
        out = []
        for rec in alerts:
            aid = rec.get("p_alert_id")
            if aid and aid in self._seen_alerts:
                continue                # already written by this worker
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
            # AZURE_CLIENT_ID selects the user-assigned identity when the app has
            # more than one; unset (system-assigned) is fine and ignored.
            cred = DefaultAzureCredential(
                managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID") or None)
            self._svc = BlobServiceClient(self._account_url, credential=cred)
        return self._svc

    def _bucket(self) -> str:
        """The current time, floored to `rollover_minutes` - what makes every
        worker/partition writing at the same moment land on the same blob."""
        now = datetime.now(timezone.utc)
        floor_minute = (now.minute // self._rollover_minutes) * self._rollover_minutes
        return now.strftime("%Y-%m-%dT%H-") + f"{floor_minute:02d}"

    def _blob_client(self, prefix: str):
        svc = self._service()
        if not self._container_ready:
            try:
                svc.create_container(self._container)
            except Exception:
                pass                    # already exists, or no permission to create
            self._container_ready = True
        blob_name = f"{prefix}/{self._bucket()}.jsonl"
        client = svc.get_blob_client(self._container, blob_name)
        if blob_name not in self._known_blobs:
            # create_append_blob() RESETS an existing blob to 0 bytes - Put Blob
            # overwrites by default. IfMissing makes it a true create-if-absent
            # (If-None-Match: *), so a blob another worker already started today
            # is never touched. Cached per blob name so a long-lived worker
            # doesn't pay this extra round trip on every batch.
            from azure.core import MatchConditions
            from azure.core.exceptions import ResourceExistsError
            try:
                client.create_append_blob(match_condition=MatchConditions.IfMissing)
            except ResourceExistsError:
                pass                    # already exists - the normal path
            self._known_blobs.add(blob_name)
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
