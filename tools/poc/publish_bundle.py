#!/usr/bin/env python3
"""Publish a detection bundle to Blob storage so the Function App picks it up.

This is the POC-sized version of `pyre publish`. The difference: `pyre publish`
only ships a bundle produced by `pyre pull` (it refuses an unversioned bundle,
deliberately, so prod can always trace a bundle back to a DaC commit). This
script publishes ANY directory, stamping a content-hash version when there is no
`.bundle-version` - which is what you want when you're publishing the curated
POC bundle in tools/poc/dac, or hand-assembling one.

Publish order is the same and it matters: upload the zip FIRST, flip the pointer
LAST, so a worker can never read a pointer to a bundle that isn't there yet.

    az login
    python tools/poc/publish_bundle.py --account-url https://<storage>.blob.core.windows.net
    python tools/poc/publish_bundle.py --account-url ... --dir .bundle   # a `pyre pull` bundle

Auth is your `az login` identity (DefaultAzureCredential); it needs
"Storage Blob Data Contributor" on the storage account.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _version(path: str) -> str:
    """The DaC commit sha if `pyre pull` stamped one, else a hash of the files
    themselves so an edited bundle still publishes as a NEW version (that change
    in version is what makes a warm worker reload)."""
    stamp = os.path.join(path, ".bundle-version")
    if os.path.exists(stamp):
        with open(stamp) as fh:
            v = fh.read().strip()
        if v:
            return v
    h = hashlib.sha256()
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            if not f.endswith((".py", ".yml", ".yaml")):
                continue
            fp = os.path.join(root, f)
            h.update(os.path.relpath(fp, path).replace("\\", "/").encode())
            with open(fp, "rb") as fh:
                h.update(fh.read())
    return "sha256-" + h.hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE, "dac"),
                    help="bundle directory to publish (default: the curated POC bundle)")
    ap.add_argument("--account-url", default=os.environ.get("BUNDLE_BLOB_ACCOUNT_URL"),
                    help="e.g. https://<storage>.blob.core.windows.net "
                         "(or set BUNDLE_BLOB_ACCOUNT_URL)")
    ap.add_argument("--container", default="detections",
                    help="must match the function's bundle container (default: detections)")
    ap.add_argument("--pointer", default="current.json")
    args = ap.parse_args()

    if not args.account_url:
        sys.exit("publish: --account-url (or BUNDLE_BLOB_ACCOUNT_URL) is required")
    if not os.path.isdir(args.dir):
        sys.exit(f"publish: no such directory: {args.dir}")

    rules = [f for _r, _d, fs in os.walk(args.dir) for f in fs if f.endswith((".yml", ".yaml"))]
    if not rules:
        sys.exit(f"publish: {args.dir} contains no .yml/.yaml - that isn't a detection bundle")

    version = _version(args.dir)
    tmpzip = os.path.join(tempfile.gettempdir(), f"pyre-bundle-{version}.zip")
    with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(args.dir):
            for f in files:
                fp = os.path.join(root, f)
                z.write(fp, os.path.relpath(fp, args.dir))

    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        sys.exit("publish: pip install azure-identity azure-storage-blob")

    cc = BlobServiceClient(args.account_url,
                           credential=DefaultAzureCredential()).get_container_client(args.container)
    try:
        cc.create_container()
    except Exception:
        pass                                    # already exists

    blob_path = f"bundles/{version}.zip"
    with open(tmpzip, "rb") as fh:
        cc.upload_blob(blob_path, fh, overwrite=True)               # 1) bundle first
    cc.upload_blob(args.pointer,
                   json.dumps({"version": version, "path": blob_path}).encode(),
                   overwrite=True)                                  # 2) pointer last

    print(f"published {len(rules)} yaml file(s) from {os.path.relpath(args.dir, REPO)}")
    print(f"  version : {version}")
    print(f"  bundle  : {args.container}/{blob_path}")
    print(f"  pointer : {args.container}/{args.pointer}")
    print("workers reload within REFRESH_INTERVAL_SECONDS (or immediately, if the "
          "Event Grid bundle_published trigger is wired up)")


if __name__ == "__main__":
    main()
