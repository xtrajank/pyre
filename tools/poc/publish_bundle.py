#!/usr/bin/env python3
"""Package a detection bundle for the Function App to pick up.

This is the POC-sized version of `pyre publish`. The difference: `pyre publish`
only ships a bundle produced by `pyre pull` (it refuses an unversioned bundle,
deliberately, so prod can always trace a bundle back to a DaC commit). This
script handles ANY directory, stamping a content-hash version when there is no
`.bundle-version` - which is what you want when you're shipping the curated POC
bundle in tools/poc/dac, or hand-assembling one.

TWO MODES, picked by whether you pass --account-url:

  OFFLINE (default) - writes the two files to dist/detections/ and tells you
      where to drop them in the portal's Storage browser. No Azure credentials,
      no CLI. This is the POC path.

          python tools/poc/publish_bundle.py

  UPLOAD - pushes them straight to Blob. Needs `az login` and "Storage Blob Data
      Contributor" on the account.

          python tools/poc/publish_bundle.py --account-url https://<storage>.blob.core.windows.net

Either way the ORDER matters and is enforced: the bundle zip lands first, the
pointer last, so a worker can never read a pointer to a bundle that isn't there
yet. When uploading by hand, upload them in the order printed.
"""
import argparse
import hashlib
import json
import os
import shutil
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
                    help="upload straight to Blob (needs az login). Omit to write the "
                         "files to --out-dir for a manual portal upload.")
    ap.add_argument("--out-dir", default=os.path.join(REPO, "dist", "detections"),
                    help="where the offline mode writes the files")
    ap.add_argument("--container", default="detections",
                    help="must match the function's bundle container (default: detections)")
    ap.add_argument("--pointer", default="current.json")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        sys.exit(f"publish: no such directory: {args.dir}")

    rules = [f for _r, _d, fs in os.walk(args.dir) for f in fs if f.endswith((".yml", ".yaml"))]
    if not rules:
        sys.exit(f"publish: {args.dir} contains no .yml/.yaml - that isn't a detection bundle")

    version = _version(args.dir)
    blob_path = f"bundles/{version}.zip"
    pointer_json = json.dumps({"version": version, "path": blob_path})

    tmpzip = os.path.join(tempfile.gettempdir(), f"pyre-bundle-{version}.zip")
    with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(args.dir):
            # Never ship __pycache__: it's bytecode compiled for whatever Python
            # built it (yours, on Windows), it bloats the bundle, and it makes the
            # zip confusing to eyeball in the portal.
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                fp = os.path.join(root, f)
                # relpath keeps the .py/.yml pairs at the ROOT of the zip, with no
                # wrapping folder. The engine walks the extracted tree, so a
                # wrapping folder still works - but it makes the portal upload
                # harder to eyeball, and it's the most common hand-zip mistake.
                z.write(fp, os.path.relpath(fp, args.dir))

    # ---- offline: write the files, print where they go in the portal ----------
    if not args.account_url:
        out = args.out_dir
        os.makedirs(os.path.join(out, "bundles"), exist_ok=True)
        zip_out = os.path.join(out, blob_path.replace("/", os.sep))
        ptr_out = os.path.join(out, args.pointer)
        shutil.copyfile(tmpzip, zip_out)
        with open(ptr_out, "w", encoding="utf-8") as fh:
            fh.write(pointer_json)

        rel = os.path.relpath(out, REPO)
        print(f"packaged {len(rules)} yaml file(s) from {os.path.relpath(args.dir, REPO)}")
        print(f"  version: {version}\n")
        print("Upload these to the container "
              f"'{args.container}' in the portal (Storage account -> Storage browser).")
        print("ORDER MATTERS - the zip first, the pointer second:\n")
        print(f"  1. {os.path.join(rel, blob_path.replace('/', os.sep))}")
        print(f"     -> upload into folder:  bundles/")
        print(f"  2. {os.path.join(rel, args.pointer)}")
        print(f"     -> upload to the root of the container, overwriting the old one\n")
        print(f"  {args.pointer} contains: {pointer_json}")
        print("\nWorkers reload within REFRESH_INTERVAL_SECONDS of the pointer changing.")
        return

    # ---- upload ---------------------------------------------------------------
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

    with open(tmpzip, "rb") as fh:
        cc.upload_blob(blob_path, fh, overwrite=True)               # 1) bundle first
    cc.upload_blob(args.pointer, pointer_json.encode(), overwrite=True)   # 2) pointer last

    print(f"published {len(rules)} yaml file(s) from {os.path.relpath(args.dir, REPO)}")
    print(f"  version : {version}")
    print(f"  bundle  : {args.container}/{blob_path}")
    print(f"  pointer : {args.container}/{args.pointer}")
    print("workers reload within REFRESH_INTERVAL_SECONDS (or immediately, if the "
          "Event Grid bundle_published trigger is wired up)")


if __name__ == "__main__":
    main()
