#!/usr/bin/env python3
"""Bundle a Detections-as-Code repo into the two files pyre reads from Blob.

DROP THIS FOLDER INTO YOUR DaC REPO. It has no dependency on the pyre repo and
needs nothing but Python (PyYAML is used for validation if it's installed, and
skipped with a warning if it isn't).

    cd <your-dac-repo>
    python dac_bundler/bundle.py

It walks the repo, validates the detections it finds, and writes:

    dist/bundles/<version>.zip     the bundle
    dist/current.json              the pointer that names it

Upload BOTH to the `detections` blob container - the zip first, the pointer
second. See the printed instructions, or docs/poc/README.md in the pyre repo.

Useful flags:
    --source rules              only bundle a subfolder
    --extra global_helpers      also include a folder outside --source
    --exclude "legacy/**"       skip paths (repeatable)
    --version v1.2.3            pin the version instead of hashing the contents
"""
import argparse
import fnmatch
import hashlib
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCE = os.path.dirname(HERE)          # the repo this folder was dropped into

# Never bundled. The bundler folder excludes itself so it can't be mistaken for a
# detection, and test files are excluded because Panther-style `*_tests.py` import
# test frameworks the engine doesn't have.
ALWAYS_EXCLUDE = [
    ".git/**", "**/.git/**",
    "**/__pycache__/**",
    "dist/**", "**/dist/**",
    ".venv/**", "**/.venv/**",
    "**/*_test.py", "**/*_tests.py",
    os.path.basename(HERE) + "/**",
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
            sys.exit(f"bundle: no such directory: {root}")
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", ".venv")]
            for fn in filenames:
                if not fn.endswith(INCLUDE_EXT):
                    continue
                full = os.path.join(dirpath, fn)
                rel = _rel(full, root)
                arc = f"{prefix}/{rel}" if prefix else rel
                if any(fnmatch.fnmatch(arc, pat) or fnmatch.fnmatch(rel, pat) for pat in excludes):
                    continue
                files.append((full, arc))
    return files


def _validate(files):
    """Catch the mistakes that produce a bundle which uploads fine and then loads
    zero detections. Returns (rule_count, helper_count, log_types, errors)."""
    try:
        import yaml
    except ImportError:
        print("bundle: PyYAML not installed - skipping validation "
              "(pip install pyyaml to enable it)\n")
        return None

    by_arc = {arc: full for full, arc in files}
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
        # YAML that isn't a detection (CI config, schemas, docs metadata); those
        # ride along in the zip harmlessly and must not be reported as broken.
        if atype not in ("rule", "global") and "RuleID" not in meta:
            ignored += 1
            continue
        if atype not in (None, "rule", "global"):
            ignored += 1               # policy / scheduled_rule / datamodel
            continue
        atype = atype or "rule"

        # The .py must sit in the SAME directory as its .yml - the engine resolves
        # Filename relative to the yml, using only its basename.
        fname = meta.get("Filename")
        if fname:
            sibling = os.path.dirname(arc)
            expect = f"{sibling}/{os.path.basename(fname)}" if sibling else os.path.basename(fname)
            if expect not in by_arc:
                errors.append(f"{arc}: Filename '{fname}' not found next to it "
                              f"(expected {expect})")

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
    which is exactly what makes a running worker reload the bundle."""
    h = hashlib.sha256()
    for full, arc in sorted(files, key=lambda p: p[1]):
        h.update(arc.encode())
        with open(full, "rb") as fh:
            h.update(fh.read())
    return "sha256-" + h.hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="folder holding the detections (default: the repo this script lives in)")
    ap.add_argument("--extra", action="append", default=[], metavar="DIR",
                    help="also include this folder, e.g. a global_helpers/ outside --source (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip paths matching this glob (repeatable)")
    ap.add_argument("--out", default=None, help="output dir (default: <cwd>/dist)")
    ap.add_argument("--version", default=None, help="pin the version instead of hashing contents")
    ap.add_argument("--pointer", default="current.json")
    args = ap.parse_args()

    source = os.path.abspath(args.source)
    out = os.path.abspath(args.out or os.path.join(os.getcwd(), "dist"))
    excludes = ALWAYS_EXCLUDE + args.exclude

    files = _collect(source, [os.path.abspath(d) for d in args.extra], excludes)
    if not files:
        sys.exit(f"bundle: found no .py/.yml files under {source}")

    result = _validate(files)
    if result:
        rules, helpers, log_types, errors, ignored = result
        for e in errors:
            print(f"  ERROR {e}")
        if errors:
            sys.exit(f"\nbundle: {len(errors)} problem(s) - fix these first. A bundle with "
                     f"these errors uploads fine and then loads nothing.")
        if not rules:
            sys.exit(f"bundle: found {len(files)} file(s) under {source} but no detections.\n"
                     f"       A detection is a .yml declaring RuleID, Filename and LogTypes,\n"
                     f"       with its .py in the SAME folder. Check --source points at them.")
        extra = f", ignored {ignored} non-detection YAML file(s)" if ignored else ""
        print(f"validated {rules} detection(s), {helpers} global helper(s){extra}\n")
        print("LogTypes declared by these detections - an event's log-type field")
        print("must hold one of these EXACTLY, or it will never be routed:")
        for lt, n in sorted(log_types.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {n:5}  {lt}")
        print()

    version = args.version or _version(files)
    blob_path = f"bundles/{version}.zip"
    pointer_json = json.dumps({"version": version, "path": blob_path})

    os.makedirs(os.path.join(out, "bundles"), exist_ok=True)
    zip_out = os.path.join(out, blob_path.replace("/", os.sep))
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for full, arc in sorted(files, key=lambda p: p[1]):
            z.write(full, arc)
    with open(os.path.join(out, args.pointer), "w", encoding="utf-8") as fh:
        fh.write(pointer_json)

    print(f"bundled {len(files)} file(s)")
    print(f"  version: {version}\n")
    print("Upload BOTH to the 'detections' blob container, IN THIS ORDER:\n")
    print(f"  1. {zip_out}")
    print(f"       -> into the folder:  bundles/")
    print(f"  2. {os.path.join(out, args.pointer)}")
    print(f"       -> to the container ROOT, overwriting the existing one")
    print(f"\n  {args.pointer} contains: {pointer_json}")
    print("\nThe zip must go first: a worker that reads a pointer to a bundle "
          "that isn't\nthere yet would fail to load detections.")


if __name__ == "__main__":
    main()
