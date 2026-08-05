"""Where the detection bundle comes from.

Detections are NOT in this repo - they live in their own Detections-as-Code
repo, which publishes a versioned zip plus a tiny pointer blob. This module is
the only place that knows how a worker gets hold of that zip.

The contract is two methods:
    current_version() -> str        cheap "did anything change?" probe
    ensure_local(version) -> str    a directory the Registry can walk

Two implementations, chosen by DETECTIONS_SOURCE:
    blob   BlobBundleSource   Azure Blob via Managed Identity. Deployed apps.
    local  LocalBundleSource  a directory on disk. Tests and local runs.
"""
import hashlib
import io
import json
import os
import tempfile
import zipfile


class LocalBundleSource:
    """A directory on disk is the bundle. The version is a content hash, so
    editing a rule locally is enough to trigger a reload."""

    def __init__(self, path: str):
        self._path = path

    def current_version(self) -> str:
        return _hash_tree(self._path)

    def ensure_local(self, version: str) -> str:
        return self._path


class BlobBundleSource:
    """A pointer blob names the live bundle:

        detections/current.json          {"version": "...", "path": "bundles/....zip"}
        detections/bundles/<version>.zip

    The worker reads the pointer each refresh and downloads the zip only when the
    version changed, caching it per version so a reload is download-once. Auth is
    Managed Identity: no key or PAT ever reaches the Function App.
    """

    def __init__(self, account_url: str, container: str, pointer_blob: str, cache_dir: str | None = None):
        self._account_url = account_url
        self._container = container
        self._pointer = pointer_blob
        self._cache = cache_dir or os.path.join(tempfile.gettempdir(), "pyre-bundles")
        self._svc = None

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

    def _read_pointer(self) -> dict:
        blob = self._service().get_blob_client(self._container, self._pointer)
        return json.loads(blob.download_blob().readall())

    def current_version(self) -> str:
        return self._read_pointer()["version"]

    def ensure_local(self, version: str) -> str:
        dest = os.path.join(self._cache, version)
        if os.path.isdir(dest) and os.listdir(dest):
            return dest
        raw = self._service().get_blob_client(
            self._container, self._read_pointer()["path"]).download_blob().readall()
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            z.extractall(dest)
        return dest


def source_from_config(cfg):
    """The one swap point between "detections from Blob" and "detections from a
    folder". DETECTIONS_SOURCE names which; a third kind is one more class and
    one more branch here."""
    if cfg.detections_source == "blob":
        return BlobBundleSource(cfg.detections_blob_account_url, cfg.detections_container,
                                cfg.detections_pointer)
    return LocalBundleSource(cfg.detections_local_dir)


def _hash_tree(path: str) -> str:
    """A fingerprint of the detection files in a directory: relative path, size
    and mtime of every .py/.yml. Cheap enough to run each refresh tick."""
    h = hashlib.sha256()
    if not os.path.isdir(path):
        return "empty"
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            if not f.endswith((".py", ".yml", ".yaml")):
                continue
            fp = os.path.join(root, f)
            st = os.stat(fp)
            h.update(os.path.relpath(fp, path).encode())
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]
