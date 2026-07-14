"""The seam between "where detections live" and "the engine that runs them".

Detections are NOT in this repo. They live in an external DaC repository (same
layout as panther-analysis: paired .py + .yml). This module is the ONLY place
that knows how a bundle of those detections is obtained. Everything downstream
(the Registry, the Processor) just reads a local directory.

Three interchangeable sources, all behind one tiny contract:

  LocalBundleSource : read a directory as-is - an Azure Files mount that CI writes
                      to, or a local `pyre pull` checkout. Offline default; tests
                      use this. No Azure, no network.
  BlobBundleSource  : pull a published bundle from Blob storage via Managed
                      Identity (no PAT in the function). The engine's prod source.
  (GitBundleSource) : cloning the DaC repo is the PUBLISH side and lives in the
                      CLI (`pyre pull`), because only publish needs the PAT. The
                      engine never clones.

The contract:
    current_version() -> str        # cheap "did anything change?" probe
    ensure_local(version) -> str    # a local dir the Registry can walk

To react to pushes differently later (Event Grid blob-written push instead of a
poll), add a new BundleSource and select it in `source_from_config`. The engine
does not change.
"""
import hashlib
import os
import tempfile

from .config import DacConfig


class BundleSource:
    """Interface. See module docstring for the two-method contract."""

    def current_version(self) -> str:
        raise NotImplementedError

    def ensure_local(self, version: str) -> str:
        raise NotImplementedError


class LocalBundleSource(BundleSource):
    """A directory on disk is the bundle. Version is a `.bundle-version` file if
    present (written by `pyre pull` = the DaC commit sha), else a content hash so
    local edits still trigger a reload."""

    def __init__(self, path: str):
        self._path = path

    def current_version(self) -> str:
        vf = os.path.join(self._path, ".bundle-version")
        if os.path.exists(vf):
            with open(vf) as fh:
                return fh.read().strip()
        return _hash_tree(self._path)

    def ensure_local(self, version: str) -> str:
        return self._path


class BlobBundleSource(BundleSource):
    """Prod engine source. A tiny pointer blob names the current version and the
    bundle zip; the worker polls the pointer and downloads a new zip only when the
    version changes. Auth is Managed Identity - no PAT ever reaches the function.

    Bundles are cached under a per-version dir so a reload is download-once."""

    def __init__(self, account_url: str, container: str, pointer_blob: str, cache_dir: str):
        self._account_url = account_url
        self._container = container
        self._pointer = pointer_blob
        self._cache = cache_dir
        self._svc = None

    def _service(self):
        if self._svc is None:
            # Lazy import so local/offline runs never need the Azure SDK installed.
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
            self._svc = BlobServiceClient(self._account_url, credential=DefaultAzureCredential())
        return self._svc

    def _read_pointer(self) -> dict:
        import json
        blob = self._service().get_blob_client(self._container, self._pointer)
        return json.loads(blob.download_blob().readall())

    def current_version(self) -> str:
        return self._read_pointer()["version"]

    def ensure_local(self, version: str) -> str:
        dest = os.path.join(self._cache, version)
        if os.path.isdir(dest) and os.listdir(dest):
            return dest
        import io
        import zipfile
        bundle_path = self._read_pointer()["path"]
        blob = self._service().get_blob_client(self._container, bundle_path)
        raw = blob.download_blob().readall()
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            z.extractall(dest)
        return dest


def source_from_config(cfg: DacConfig) -> BundleSource:
    """Pick the bundle source from config. This one function is the swap point."""
    if cfg.bundle_mode == "blob":
        return BlobBundleSource(
            cfg.blob_account_url, cfg.blob_container, cfg.blob_pointer,
            cache_dir=os.path.join(tempfile.gettempdir(), "pyre-bundles"),
        )
    return LocalBundleSource(cfg.local_dir)


def _hash_tree(path: str) -> str:
    """A stable fingerprint of the detection files in a dir - relpath + size +
    mtime of every .py/.yml. Cheap enough to run each refresh tick."""
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
            h.update(str(st.st_size).encode())
            h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()[:16]
