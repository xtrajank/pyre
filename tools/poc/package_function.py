#!/usr/bin/env python3
"""Build a zip you can upload to the Function App by hand, with no CLI.

Core Tools (`func azure functionapp publish`) normally builds the Python
dependencies ON the Linux worker, so you never have to produce Linux wheels
yourself. Without it, the zip has to arrive complete - so this script vendors the
dependencies into `.python_packages/lib/site-packages/`, which is exactly where
the Linux Python worker looks. The result needs no build step on the far side and
can be dropped straight into the portal (Kudu -> Zip Push Deploy).

    python tools/poc/package_function.py
    # -> dist/pyre-poc.zip, then upload it in the portal (docs/poc/README.md step 4)

    # offline detections instead of reading them from Blob (BUNDLE_MODE=local):
    python tools/poc/package_function.py --with-bundle tools/poc/dac

Needs pip able to reach PyPI. `--skip-deps` produces a code-only zip, which is
valid ONLY if the app already has its dependencies from a previous deploy.
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ENGINE = os.path.join(REPO, "engine")

# The Flex Consumption / Linux Python worker this POC targets.
PY_VERSION = "3.11"
PLATFORM = "manylinux2014_x86_64"

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".python_packages", ".venv"}
SKIP_FILES = {"local.settings.json", "registry_index.json"}


def _vendor_deps(dest: str) -> None:
    """pip install the engine's requirements as LINUX wheels, from any OS.

    --only-binary=:all: is what makes this cross-platform-safe: it refuses to
    build from source (which would produce wheels for the machine you're on) and
    fails loudly instead of silently packaging something the worker can't load."""
    print(f"vendoring dependencies -> {os.path.relpath(dest, REPO)}")
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", dest,
        "--platform", PLATFORM,
        "--python-version", PY_VERSION,
        "--implementation", "cp",
        "--only-binary=:all:",
        "--upgrade",
        "-r", os.path.join(ENGINE, "requirements.txt"),
    ]
    if subprocess.call(cmd) != 0:
        sys.exit("package: pip install failed - check network/proxy access to PyPI. "
                 "Re-run with --skip-deps only if the app already has its dependencies.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(REPO, "dist", "pyre-poc.zip"))
    ap.add_argument("--with-bundle", default=None, metavar="DIR",
                    help="also embed a detection bundle at .bundle/ inside the zip "
                         "(pair with app settings BUNDLE_MODE=local, BUNDLE_LOCAL_DIR=.bundle)")
    ap.add_argument("--skip-deps", action="store_true",
                    help="code only; the app must already have its dependencies installed")
    args = ap.parse_args()

    staging = os.path.join(REPO, "dist", "_staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    # 1. The function app itself. engine/ IS the app root: function_app.py,
    #    host.json, requirements.txt and the pyre_engine package sit at the top
    #    level of the zip, which is what the worker expects.
    for entry in os.listdir(ENGINE):
        if entry in SKIP_DIRS or entry in SKIP_FILES:
            continue
        src, dst = os.path.join(ENGINE, entry), os.path.join(staging, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        else:
            shutil.copy2(src, dst)

    # 2. The declarative config the engine reads at runtime: which destinations
    #    exist, and which hubs to attach triggers to. The POC doesn't need either
    #    (it has no destinations and one hub via EVENTHUB_NAME), but shipping them
    #    always is what makes going to production a SETTINGS change rather than a
    #    repackage: turning on Torq or adding a hub is then an edit to these files
    #    plus app settings, with the same build step.
    shutil.copytree(os.path.join(REPO, "config"), os.path.join(staging, "config"),
                    ignore=shutil.ignore_patterns(*SKIP_DIRS))

    # 3. Optional offline detections. Only needed if you're NOT publishing the
    #    bundle to Blob; note this pins detections to the deploy, giving up the
    #    hot-reload the Blob path buys you.
    if args.with_bundle:
        src = args.with_bundle if os.path.isabs(args.with_bundle) else os.path.join(REPO, args.with_bundle)
        if not os.path.isdir(src):
            sys.exit(f"package: --with-bundle: no such directory: {src}")
        shutil.copytree(src, os.path.join(staging, ".bundle"),
                        ignore=shutil.ignore_patterns(*SKIP_DIRS))
        print(f"embedded detections from {os.path.relpath(src, REPO)} -> .bundle/")

    # 4. Dependencies, where the Linux worker looks for them.
    if not args.skip_deps:
        _vendor_deps(os.path.join(staging, ".python_packages", "lib", "site-packages"))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(staging):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                fp = os.path.join(root, f)
                z.write(fp, os.path.relpath(fp, staging))
    shutil.rmtree(staging, ignore_errors=True)

    mb = os.path.getsize(args.out) / (1024 * 1024)
    print(f"\n{os.path.relpath(args.out, REPO)}  ({mb:.1f} MB)")
    print("\nUpload it in the portal:")
    print("  Function App -> Development Tools -> Advanced Tools -> Go")
    print("  -> Tools -> Zip Push Deploy -> drag the file on")
    print("  then Restart the app from Overview.")
    print("\n(See docs/poc/README.md step 4 for the Flex Consumption alternative.)")


if __name__ == "__main__":
    main()
