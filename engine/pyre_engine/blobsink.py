"""Append-blob sink - the POC's stand-in for the Cribl lake and for Torq.

Production writes signals/alerts back to a Cribl HTTP source and dispatches
alerts to a case tool. Neither exists in the POC (no external communication),
so instead every record is appended, one JSON object per line, to a blob you
can open in the portal and read top to bottom.

An APPEND blob is exactly the right primitive here: appending is a single
server-side operation with no read-modify-write, so concurrent workers can't
clobber each other, and nothing already written is ever rewritten. That is the
"append the log to the end" behaviour, done the way the storage service
actually supports it.

Layout: one blob per UTC day per stream, e.g.

    <container>/alerts/2026-07-30.jsonl
    <container>/signals/2026-07-30.jsonl

Auth is Managed Identity (DefaultAzureCredential), so no keys reach the app.
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("pyre.blobsink")

# Append blobs cap a single append at 4 MiB. Stay well under it so a big batch
# of signals is split rather than rejected.
_MAX_APPEND_BYTES = 2 * 1024 * 1024


class AppendBlobSink:
    """One stream (a prefix) inside one container. Cheap to construct - the
    Azure client is built lazily on first write, so an unused sink never costs
    a credential fetch or a network call."""

    def __init__(self, account_url: str, container: str, prefix: str):
        self._account_url = account_url
        self._container = container
        self._prefix = prefix.strip("/")
        self._svc = None
        self._container_ready = False

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

    def _blob_client(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        name = f"{self._prefix}/{day}.jsonl" if self._prefix else f"{day}.jsonl"
        svc = self._service()
        if not self._container_ready:
            # Create-if-missing keeps the POC to one setup step; the container is
            # the only thing the engine needs that Terraform isn't creating here.
            try:
                svc.create_container(self._container)
            except Exception:
                pass                    # already exists, or no create permission
            self._container_ready = True
        client = svc.get_blob_client(self._container, name)
        try:
            client.create_append_blob()  # first write of the day
        except Exception:
            pass                        # already exists - the normal path
        return client

    def append(self, records: list[dict]) -> None:
        """Append each record as its own JSON line. Never raises: losing the
        visualisation must not fail the batch that produced it (Event Hubs would
        redeliver and re-alert). Failures surface in Application Insights."""
        if not records or not self._account_url:
            return
        try:
            client = self._blob_client()
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
            log.exception("append-blob write failed for %s/%s (%d records dropped)",
                          self._container, self._prefix, len(records))
