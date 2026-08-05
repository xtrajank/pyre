#!/usr/bin/env python3
"""Rebuild this detections repo into a bundle, and publish it to Blob storage.

    python publish.py                          # build + validate only -> dist/
    python publish.py --upload https://<account>.blob.core.windows.net

`--upload` is the whole point: the DaC repo deploys itself. Run it from a
pipeline (see azure-pipelines.yml) and pushing a rule to main is all it takes for
running workers to pick it up - no Function App deploy, no portal.

Without `--upload` it writes the two files and tells you where to drop them in
the portal, which is the no-tooling path for a first run.

What it produces:

    dist/bundles/sha256-xxxxxxxx.zip     the bundle: every .py/.yml in this repo
    dist/current.json                    {"version": "...", "path": "bundles/..."}

The version is a content hash, so it changes whenever a rule changes - and a
changed version is exactly what makes a running worker reload.

It VALIDATES before it publishes, and refuses to ship a bundle that would load
nothing: a `.py` in the wrong folder, a missing RuleID, unparseable YAML.

Useful flags:
    --source rules              bundle a subfolder instead of the whole repo
    --extra global_helpers      also include a folder outside --source
    --exclude "legacy/**"       skip paths (repeatable)
    --version v1.2.3            pin the version instead of hashing contents
"""
import argparse
import fnmatch
import hashlib
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Never bundled. Test files are excluded because Panther-style `*_tests.py`
# import test frameworks the engine doesn't have.
ALWAYS_EXCLUDE = [
    ".git/**", "**/.git/**",
    "**/__pycache__/**",
    "dist/**", "**/dist/**",
    ".venv/**", "**/.venv/**",
    "**/*_test.py", "**/*_tests.py",
    "publish.py", "tests/**",
]
INCLUDE_EXT = (".py", ".yml", ".yaml")


def _rel(path, root):
    return os.path.relpath(path, root).replace("\\", "/")


def _collect(source, extra_dirs, excludes):
    """Every .py/.yml/.yaml under source (plus extra dirs), as
    (absolute path, path-inside-the-zip) pairs."""
    files = []
    roots = [(source, "")] + [(d, os.path.basename(d.rstrip("/\\"))) for d in extra_dirs]
    for root, prefix in roots:
        if not os.path.isdir(root):
            sys.exit(f"publish: no such directory: {root}")
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", ".venv")]
            for fn in filenames:
                if not fn.endswith(INCLUDE_EXT):
                    continue
                full = os.path.join(dirpath, fn)
                rel = _rel(full, root)
                arc = f"{prefix}/{rel}" if prefix else rel
                if any(fnmatch.fnmatch(arc, p) or fnmatch.fnmatch(rel, p) for p in excludes):
                    continue
                files.append((full, arc))
    return files


def _validate(files):
    """Catch the mistakes that produce a bundle which uploads fine and then loads
    zero detections. Returns (rules, helpers, log_types, errors, ignored)."""
    try:
        import yaml
    except ImportError:
        print("publish: PyYAML not installed - skipping validation "
              "(pip install pyyaml to enable it)\n")
        return None

    by_arc = {arc for _full, arc in files}
    rules, helpers, log_types, errors, ignored = 0, 0, {}, [], 0

    for full, arc in files:
        if not arc.endswith((".yml", ".yaml")):
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                meta = yaml.safe_load(fh)
        except Exception as e:
            errors.append(f"{arc}: unreadable YAML ({type(e).__name__})")
            continue
        if not isinstance(meta, dict):
            continue

        atype = meta.get("AnalysisType")
        # Mirror the engine's own rule for "is this a detection?" exactly, so the
        # bundler never disagrees with what will actually load. A repo is full of
        # YAML that isn't a detection (CI config, schemas); it rides along in the
        # zip harmlessly and must not be reported as broken.
        if atype not in ("rule", "global") and "RuleID" not in meta:
            ignored += 1
            continue
        if atype not in (None, "rule", "global"):
            ignored += 1                        # policy / scheduled_rule / datamodel
            continue
        atype = atype or "rule"

        # The .py must sit in the SAME directory as its .yml: the engine resolves
        # Filename next to the YAML, using only its basename.
        fname = meta.get("Filename")
        if fname:
            sibling = os.path.dirname(arc)
            expect = f"{sibling}/{os.path.basename(fname)}" if sibling else os.path.basename(fname)
            if expect not in by_arc:
                errors.append(f"{arc}: Filename '{fname}' not found next to it (expected {expect})")

        if atype == "global":
            if not fname:
                errors.append(f"{arc}: AnalysisType global needs Filename")
            helpers += 1
            continue

        rules += 1
        for key in ("RuleID", "Filename", "LogTypes"):
            if key not in meta:
                errors.append(f"{arc}: missing required key '{key}'")
        for lt in meta.get("LogTypes") or []:
            log_types[lt] = log_types.get(lt, 0) + 1

    return rules, helpers, log_types, errors, ignored


def _version(files):
    """Content hash of the detections. Changing any rule changes the version,
    which is what makes a running worker reload the bundle."""
    h = hashlib.sha256()
    for full, arc in sorted(files, key=lambda p: p[1]):
        h.update(arc.encode())
        with open(full, "rb") as fh:
            h.update(fh.read())
    return "sha256-" + h.hexdigest()[:16]


def _upload(account_url, container, zip_path, arc_path, pointer_name, pointer_json):
    """Push the bundle then the pointer, in that order.

    The order is the whole reason publishing is two files: a worker must never be
    able to read a pointer to a bundle that isn't there yet.

    Auth is DefaultAzureCredential - a pipeline's service connection, or your own
    signed-in identity locally. It needs `Storage Blob Data Contributor` on the
    account.
    """
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    svc = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    try:
        svc.create_container(container)
        print(f"created container {container}")
    except Exception:
        pass                                    # already exists, or no permission to create

    with open(zip_path, "rb") as fh:
        svc.get_blob_client(container, arc_path).upload_blob(fh, overwrite=True)
    print(f"uploaded {container}/{arc_path}")

    svc.get_blob_client(container, pointer_name).upload_blob(
        pointer_json.encode("utf-8"), overwrite=True)
    print(f"uploaded {container}/{pointer_name}  ->  {pointer_json}")
    print("\nPublished. Warm workers pick this up within DETECTIONS_REFRESH_SECONDS; "
          "check /health for the new bundle_version.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=HERE,
                    help="folder holding the detections (default: this repo)")
    ap.add_argument("--extra", action="append", default=[], metavar="DIR",
                    help="also include this folder, e.g. a global_helpers/ outside --source")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip paths matching this glob (repeatable)")
    ap.add_argument("--out", default=None, help="output dir (default: <cwd>/dist)")
    ap.add_argument("--version", default=None, help="pin the version instead of hashing contents")
    # Deliberately has no environment-variable default: publishing is an action
    # with a blast radius, and it should never happen because a shell happened to
    # have a variable set. The pipeline passes it explicitly.
    ap.add_argument("--upload", default="", metavar="ACCOUNT_URL",
                    help="publish to https://<account>.blob.core.windows.net")
    ap.add_argument("--container", default="detections")
    ap.add_argument("--pointer", default="current.json")
    args = ap.parse_args()

    source = os.path.abspath(args.source)
    out = os.path.abspath(args.out or os.path.join(os.getcwd(), "dist"))
    files = _collect(source, [os.path.abspath(d) for d in args.extra],
                     ALWAYS_EXCLUDE + args.exclude)
    if not files:
        sys.exit(f"publish: found no .py/.yml files under {source}")

    result = _validate(files)
    if result:
        rules, helpers, log_types, errors, ignored = result
        for e in errors:
            print(f"  ERROR {e}")
        if errors:
            sys.exit(f"\npublish: {len(errors)} problem(s) - fix these first. A bundle with "
                     f"these errors uploads fine and then loads nothing.")
        if not rules:
            sys.exit(f"publish: found {len(files)} file(s) under {source} but no detections.\n"
                     f"         A detection is a .yml declaring RuleID, Filename and LogTypes,\n"
                     f"         with its .py in the SAME folder.")
        extra = f", ignored {ignored} non-detection YAML file(s)" if ignored else ""
        print(f"validated {rules} detection(s), {helpers} global helper(s){extra}\n")
        print("LogTypes declared by these detections. An event's log_type_field")
        print("must hold one of these EXACTLY, or it will never be routed:")
        for lt, n in sorted(log_types.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {n:5}  {lt}")
        print()

    version = args.version or _version(files)
    arc_path = f"bundles/{version}.zip"
    pointer_json = json.dumps({"version": version, "path": arc_path})

    os.makedirs(os.path.join(out, "bundles"), exist_ok=True)
    zip_path = os.path.join(out, arc_path.replace("/", os.sep))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for full, arc in sorted(files, key=lambda p: p[1]):
            z.write(full, arc)
    with open(os.path.join(out, args.pointer), "w", encoding="utf-8") as fh:
        fh.write(pointer_json)

    print(f"bundled {len(files)} file(s) as {version}\n")

    if args.upload:
        _upload(args.upload, args.container, zip_path, arc_path, args.pointer, pointer_json)
        return

    print(f"Upload BOTH to the '{args.container}' blob container, IN THIS ORDER:\n")
    print(f"  1. {zip_path}\n       -> into the folder:  bundles/")
    print(f"  2. {os.path.join(out, args.pointer)}\n       -> the container ROOT, overwriting")
    print(f"\n  {args.pointer} contains: {pointer_json}")
    print("\nThe zip goes first: a worker that read a pointer to a bundle which "
          "isn't there\nyet would fail to load detections.")
    print("\nOr let this script do it:  python publish.py --upload "
          "https://<account>.blob.core.windows.net")


if __name__ == "__main__":
    main()
