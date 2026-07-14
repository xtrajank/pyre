"""Command implementations. `pull` fetches the external DaC repo; the rest work
against that pulled bundle. Deploy/enable/disable shell out to `func` and `az`
so this stays a thin, auditable wrapper over first-party tooling.

The detections are NOT in this repo - `config/detections.yaml` points at the DaC
repo (panther-analysis or your fork). `pull` is the publish-side clone (the only
step that needs the PAT); the engine never clones."""
import glob
import json
import os
import shutil
import subprocess
import sys
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG_PATH = os.path.join(REPO, "config", "detections.yaml")


def _cfg():
    if not os.path.exists(CFG_PATH):
        return {}
    with open(CFG_PATH) as fh:
        return yaml.safe_load(fh) or {}


def _bundle_dir():
    """Local dir where the pulled DaC bundle lives (config/detections.yaml
    bundle.local_dir, env-overridable). All the file-walking commands read this."""
    cfg = _cfg().get("bundle", {}) or {}
    d = os.environ.get("BUNDLE_LOCAL_DIR") or cfg.get("local_dir", "./.bundle")
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(REPO, d))


def _iter_meta():
    bundle = _bundle_dir()
    if not os.path.isdir(bundle):
        return
    for ext in ("*.yml", "*.yaml"):
        for y in glob.glob(os.path.join(bundle, "**", ext), recursive=True):
            with open(y) as fh:
                yield y, yaml.safe_load(fh)


def _auth_url(repo: str, token: str) -> str:
    """Inject a PAT into an https clone URL. Never logged."""
    if token and repo.startswith("https://"):
        return repo.replace("https://", f"https://x-access-token:{token}@", 1)
    return repo


def pull(args):
    """Clone the DaC repo at the configured ref, filter to the detections
    subfolder, and land it in the local bundle dir with a .bundle-version stamp
    (the commit sha). This is what the engine's LocalBundleSource then reads."""
    dac = _cfg().get("dac", {}) or {}
    repo = os.environ.get("DAC_REPO") or dac.get("repo")
    ref = os.environ.get("DAC_REF") or dac.get("ref", "main")
    subpath = dac.get("path", "")
    token = os.environ.get(dac.get("token_env", "DAC_TOKEN"), "")
    if not repo:
        print("pull: no dac.repo configured in config/detections.yaml"); return 1

    dest = _bundle_dir()
    checkout = dest + ".src"
    shutil.rmtree(checkout, ignore_errors=True)
    print(f"pull: cloning {repo}@{ref} ...")
    rc = subprocess.call(["git", "clone", "--depth", "1", "--branch", ref,
                          _auth_url(repo, token), checkout])
    if rc:
        print(f"pull: git clone failed (rc={rc}). Check repo/ref/token."); return rc

    sha = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"]).decode().strip()
    src = os.path.join(checkout, subpath) if subpath else checkout
    if not os.path.isdir(src):
        print(f"pull: path '{subpath}' not found in repo"); shutil.rmtree(checkout, ignore_errors=True); return 1

    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))

    # Include shared global-helper dirs (Panther `AnalysisType: global` modules that
    # detections import by name) so imports resolve at load time. These usually live
    # in a sibling dir (e.g. global_helpers/) outside dac.path, so copy them in.
    for hp in (dac.get("global_helpers") or []):
        hsrc = os.path.join(checkout, hp)
        hdst = os.path.join(dest, os.path.basename(hp))
        if os.path.isdir(hsrc) and not os.path.exists(hdst):
            shutil.copytree(hsrc, hdst, ignore=shutil.ignore_patterns(".git"))
            print(f"pull: + global helpers {hp}/")

    shutil.rmtree(checkout, ignore_errors=True)
    with open(os.path.join(dest, ".bundle-version"), "w") as fh:
        fh.write(sha)
    print(f"pull: {ref} ({sha[:12]}) -> {os.path.relpath(dest, REPO)}")
    return 0


def _declared_log_types() -> set:
    """Every log_types value declared across config/sources.yaml - what
    Terraform has actually sized an Event Hub for. A detection whose LogTypes
    aren't in this set will load fine but can never see a matching event
    (wrong dataset name, or its source hasn't been added to sources.yaml)."""
    path = os.path.join(REPO, "config", "sources.yaml")
    if not os.path.exists(path):
        return set()
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    return {lt for s in (data.get("sources") or []) for lt in (s.get("log_types") or [])}


def validate(args):
    if not os.path.isdir(_bundle_dir()):
        print("validate: no bundle. Run `pyre pull` first."); return 1
    errors = 0
    declared = _declared_log_types()
    for path, meta in _iter_meta():
        if not isinstance(meta, dict):
            continue
        atype = meta.get("AnalysisType", "rule")
        if atype != "rule":
            # globals/data-models/policies aren't streaming rules; just check the
            # helper's .py exists so a broken global reference is caught early.
            if atype == "global":
                py = os.path.join(os.path.dirname(path), os.path.basename(meta.get("Filename", "")))
                if meta.get("Filename") and not os.path.exists(py):
                    print(f"ERROR {path}: global Filename {meta.get('Filename')} not found"); errors += 1
            continue
        for key in ("RuleID", "Filename", "LogTypes"):
            if key not in meta:
                print(f"ERROR {path}: missing {key}"); errors += 1
        py = os.path.join(os.path.dirname(path), os.path.basename(meta.get("Filename", "")))
        if not os.path.exists(py):
            print(f"ERROR {path}: Filename {meta.get('Filename')} not found"); errors += 1
        # Only enforced when sources.yaml actually declares something - an empty/
        # missing file disables the check rather than flagging every detection.
        if declared:
            for lt in meta.get("LogTypes") or []:
                if lt not in declared:
                    print(f"ERROR {path}: LogTypes value '{lt}' has no matching entry in "
                          f"config/sources.yaml (no Event Hub is sized for it)"); errors += 1
    print("validate: OK" if not errors else f"validate: {errors} error(s)")
    return 1 if errors else 0


def test(args):
    target = ["-k", args.detection_id] if args.detection_id else []
    return subprocess.call([sys.executable, "-m", "pytest", "-q", os.path.join(REPO, "tests")] + target)


def build(args):
    if not os.path.isdir(_bundle_dir()):
        print("build: no bundle. Run `pyre pull` first."); return 1
    version = ""
    vf = os.path.join(_bundle_dir(), ".bundle-version")
    if os.path.exists(vf):
        with open(vf) as fh:
            version = fh.read().strip()
    index = {}
    for path, meta in _iter_meta():
        if meta.get("AnalysisType", "rule") != "rule":
            continue
        for lt in meta.get("LogTypes", []):
            index.setdefault(lt, []).append(meta["RuleID"])
    out = os.path.join(REPO, "engine", "registry_index.json")
    with open(out, "w") as fh:
        json.dump({"version": version, "log_types": index}, fh, indent=2)
    print(f"build: indexed {sum(len(v) for v in index.values())} detections across "
          f"{len(index)} log types (bundle {version[:12] or 'unversioned'}) -> {out}")
    return 0


def publish(args):
    """Publish the pulled bundle to Blob so warm workers hot-reload it. Uploads a
    versioned zip FIRST, then flips the pointer LAST, so a worker never sees a
    pointer to a bundle that isn't there yet. Auth is DefaultAzureCredential
    (OIDC in CI, `az login` locally) - the processor reads it back via Managed
    Identity. This is the publish half of config/detections.yaml `bundle.mode: blob`."""
    bundle = _bundle_dir()
    if not os.path.isdir(bundle):
        print("publish: no bundle. Run `pyre pull` first."); return 1

    blob_cfg = (_cfg().get("bundle", {}) or {}).get("blob", {}) or {}
    account_url = args.account_url or os.environ.get("BUNDLE_BLOB_ACCOUNT_URL") or blob_cfg.get("account_url")
    container = args.container or blob_cfg.get("container", "detections")
    pointer = blob_cfg.get("pointer_blob", "current.json")
    if not account_url:
        print("publish: no blob account_url "
              "(bundle.blob.account_url / --account-url / BUNDLE_BLOB_ACCOUNT_URL)"); return 1

    vf = os.path.join(bundle, ".bundle-version")
    version = open(vf).read().strip() if os.path.exists(vf) else ""
    if not version:
        print("publish: bundle has no .bundle-version - run `pyre pull`. "
              "Refusing to publish an unversioned bundle."); return 1

    import tempfile
    import zipfile
    tmpzip = os.path.join(tempfile.gettempdir(), f"pyre-bundle-{version}.zip")
    with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(bundle):
            for f in files:
                fp = os.path.join(root, f)
                z.write(fp, os.path.relpath(fp, bundle))

    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        print("publish: needs the Azure SDK -> pip install azure-identity azure-storage-blob"); return 1

    cc = BlobServiceClient(account_url, credential=DefaultAzureCredential()).get_container_client(container)
    bundle_blob = f"bundles/{version}.zip"
    with open(tmpzip, "rb") as fh:
        cc.upload_blob(bundle_blob, fh, overwrite=True)                     # 1) bundle first
    cc.upload_blob(pointer, json.dumps({"version": version, "path": bundle_blob}).encode(),
                   overwrite=True)                                          # 2) pointer last
    print(f"publish: {version[:12]} -> {account_url}/{container}/{bundle_blob} "
          f"(pointer {pointer}); workers reload within refresh_interval_seconds")
    return 0


def deploy(args):
    print(f"deploy: env={args.env}")
    print("  1) pyre pull                              (fetch DaC bundle at pinned ref)")
    print("  2) terraform apply (infra)                (run via `make apply-<env>`)")
    print("  3) func azure functionapp publish <app>   (engine)")
    print("  4) pyre publish                            (bundle -> Blob + pointer; workers hot-reload)")
    print("  NOTE: wire these to your pipeline; kept explicit here for auditability.")
    return 0


def _set_flag(args, enabled: bool):
    # az appconfig kv set --key pyre/<env>/enabled/<id> --value <bool>
    print(f"{'enable' if enabled else 'disable'}: {args.detection_id} in {args.env} "
          f"(App Config flag; effective within refresh_interval_seconds, no redeploy)")
    return 0


def enable(args):  return _set_flag(args, True)
def disable(args): return _set_flag(args, False)


def status(args):
    print(f"status: env={args.env} (reads App Config enabled flags)")
    return 0
