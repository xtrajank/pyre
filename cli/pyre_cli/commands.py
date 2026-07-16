"""Command implementations. `pull` fetches the external DaC repo; the rest work
against that pulled bundle. Deploy/enable/disable shell out to `func` and `az`
so this stays a thin, auditable wrapper over first-party tooling.

The detections are NOT in this repo - `config/detections.yaml` points at the DaC
repo (panther-analysis or your fork). `pull` is the publish-side clone (the only
step that needs the PAT); the engine never clones."""
import base64
import glob
import json
import os
import shutil
import stat
import subprocess
import sys
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cfg_path() -> str:
    """Where the DaC pointer lives. Env-overridable under the SAME name the
    engine already honours (engine/pyre_engine/config.py load_dac_config), so the
    CLI and the engine can be pointed at one config together."""
    return os.environ.get("DETECTIONS_CONFIG_PATH") or os.path.join(REPO, "config", "detections.yaml")


def _cfg():
    path = _cfg_path()
    if not os.path.exists(path):
        return {}
    # Binary mode: PyYAML decodes UTF-8 (and BOMs) itself. Text mode would use the
    # platform locale encoding - cp1252 on Windows - and choke on non-ASCII.
    with open(path, "rb") as fh:
        return yaml.safe_load(fh) or {}


def _rmtree(path: str) -> None:
    """Delete a tree, including files git marked read-only.

    `git clone` writes .git/objects/**/*.pack|idx read-only. On Windows the
    read-only bit makes unlink fail with PermissionError, so a plain
    shutil.rmtree(ignore_errors=True) SILENTLY leaves .bundle.src/.git behind -
    and the next `pyre pull` then fails, because git refuses to clone into a
    non-empty directory. Clear the bit and retry the unlink instead.

    Raises OSError if a path genuinely can't be removed (e.g. a file is open in
    another process); callers report that rather than swallowing it.
    """
    if not os.path.exists(path):
        return

    def _clear_readonly(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    # rmtree's error hook was renamed onerror -> onexc in 3.12 (onerror removed in
    # 3.14). The handler ignores its third arg, so it fits both signatures.
    kw = {"onexc": _clear_readonly} if sys.version_info >= (3, 12) else {"onerror": _clear_readonly}
    shutil.rmtree(path, **kw)


def _bundle_dir():
    """Local dir where the pulled DaC bundle lives (config/detections.yaml
    bundle.local_dir, env-overridable). All the file-walking commands read this."""
    cfg = _cfg().get("bundle", {}) or {}
    d = os.environ.get("BUNDLE_LOCAL_DIR") or cfg.get("local_dir", "./.bundle")
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(REPO, d))


def _iter_meta():
    """(path, meta) for every detection YAML in the bundle. Empty and malformed
    files are skipped rather than yielded as None, so callers can just .get()."""
    bundle = _bundle_dir()
    if not os.path.isdir(bundle):
        return
    for ext in ("*.yml", "*.yaml"):
        for y in glob.glob(os.path.join(bundle, "**", ext), recursive=True):
            with open(y, "rb") as fh:        # see _cfg(): PyYAML handles the decode
                meta = yaml.safe_load(fh)
            if isinstance(meta, dict):
                yield y, meta


def _clone_env(repo: str, token: str) -> dict:
    """Environment that authenticates `git clone` WITHOUT the PAT ever appearing
    in argv, in the URL, or on disk.

    The token used to be spliced into the clone URL
    (https://x-access-token:<PAT>@host/repo), which leaks it two ways (both
    verified against git 2.47, which does at least redact the URL in its own
    error messages):
      * argv is readable by any other process on the agent (`ps auxww`); and
      * git PERSISTS that URL verbatim into the checkout's .git/config. That
        file lived under .bundle.src, which this command deletes on the way out
        - but a pull that died before the cleanup (or, until it was fixed, one
        whose cleanup silently failed on git's read-only pack files) left a PAT
        sitting on disk indefinitely.
    CI secret-masking is a mitigation, not a fix - the value still left us.

    Instead the URL stays clean and the credential rides an Authorization header
    injected via git's env-based config (GIT_CONFIG_COUNT/KEY/VALUE, git 2.31+).
    Env vars are not in argv and are not logged. This is the same mechanism Azure
    DevOps' own checkout task uses (http.extraheader).
    """
    env = dict(os.environ)
    if not token or not repo.startswith("https://"):
        return env
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    env["GIT_TERMINAL_PROMPT"] = "0"   # fail instead of hanging on a cred prompt
    return env


def _prune(dest: str, include: list, exclude: list) -> int:
    """Apply config/detections.yaml's dac.include / dac.exclude to a pulled bundle.

    Patterns are glob, relative to the bundle root, `**` matching any depth
    (e.g. `**/*_tests.py`). A file is kept when it matches SOME include pattern
    and NO exclude pattern.

    These two settings were parsed into DacConfig and then used by nothing at
    all: `pull` copied the whole of dac.path verbatim, so the documented
    `exclude: ["**/*_tests.py"]` never excluded anything. That matters for more
    than tidiness - the bundle is what every worker downloads and what the
    Registry YAML-parses on each reload, so it is a real cost lever, and it is
    the only way to drop a detection the engine can't run without narrowing
    dac.path wholesale.

    Returns the number of files removed.
    """
    def matching(patterns):
        hits = set()
        for pat in patterns or []:
            # recursive=True is what makes `**` match zero-or-more directories,
            # so `**/*.yml` covers a file at the bundle root too. fnmatch would
            # not: its `*` spans separators and `**/x` would demand a directory.
            hits.update(glob.glob(pat, root_dir=dest, recursive=True))
        return {h.replace(os.sep, "/") for h in hits}

    keep = matching(include) if include else None      # None = keep everything
    drop = matching(exclude)
    removed = 0
    for root, _dirs, files in os.walk(dest, topdown=False):
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, dest).replace(os.sep, "/")
            if rel == ".bundle-version":
                continue
            if (keep is not None and rel not in keep) or rel in drop:
                os.remove(fp)
                removed += 1
        # Prune directories the removals just emptied (topdown=False = children
        # first, so an emptied parent is still caught on the way up).
        if root != dest and not os.listdir(root):
            os.rmdir(root)
    return removed


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
    try:
        _rmtree(checkout)                    # a leftover checkout makes git clone fail
    except OSError as e:
        print(f"pull: cannot remove the stale checkout {checkout}: {e}\n"
              f"pull: something still has a file open in it; close it and retry."); return 1
    print(f"pull: cloning {repo}@{ref} ...")
    # The URL passed to git carries NO credential (see _clone_env), so neither
    # argv, git's error output, nor the checkout's .git/config can leak the PAT.
    rc = subprocess.call(["git", "clone", "--depth", "1", "--branch", ref, repo, checkout],
                         env=_clone_env(repo, token))
    if rc:
        print(f"pull: git clone failed (rc={rc}). Check repo/ref/token."); return rc

    sha = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"]).decode().strip()
    src = os.path.join(checkout, subpath) if subpath else checkout
    if not os.path.isdir(src):
        print(f"pull: path '{subpath}' not found in repo"); _rmtree(checkout); return 1

    # copytree preserves mode bits, so a previous bundle can hold read-only files
    # too - same forceful delete, and it must actually succeed or copytree below
    # fails on the existing dir.
    try:
        _rmtree(dest)
    except OSError as e:
        print(f"pull: cannot replace the existing bundle {dest}: {e}"); return 1
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

    # Apply dac.include/dac.exclude AFTER the global helpers are copied in, so a
    # narrow include (e.g. only your own rules dir) can't strip the helpers those
    # rules import - which would load-fail every one of them at runtime.
    removed = _prune(dest, dac.get("include") or [], dac.get("exclude") or [])
    if removed:
        print(f"pull: - {removed} file(s) filtered out by dac.include/dac.exclude")

    try:
        _rmtree(checkout)
    except OSError as e:
        # The bundle itself is already good, so don't fail the pull - but say so,
        # because a silent leftover here is exactly what broke the next pull.
        print(f"pull: warning: could not remove {checkout}: {e}")
    with open(os.path.join(dest, ".bundle-version"), "w") as fh:
        fh.write(sha)
    print(f"pull: {ref} ({sha[:12]}) -> {os.path.relpath(dest, REPO)}")
    return 0


def _sources() -> list:
    """Flat list of hub entries across every namespace in config/sources.yaml,
    each as {hub, log_types, default} - the shape the rest of this file uses."""
    path = os.path.join(REPO, "config", "sources.yaml")
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:        # see _cfg(): PyYAML handles the decode
        data = yaml.safe_load(fh) or {}
    return [
        {"hub": h.get("name"), "log_types": h.get("log_types") or [], "default": h.get("default", False)}
        for ns in (data.get("namespaces") or [])
        for h in (ns.get("hubs") or [])
    ]


def _declared_log_types() -> set:
    """Every log_types value named across config/sources.yaml - the log types
    that have a hub explicitly sized for them."""
    return {lt for s in _sources() for lt in (s.get("log_types") or [])}


def _default_hub() -> str | None:
    """The catch-all hub (config/sources.yaml `default: true`), if any.

    Its existence is what makes an undeclared log type legitimate rather than an
    error: Cribl sends anything without a dedicated hub there, the processor
    consumes it like any other hub, and routing to detections is by the event's
    log-type field regardless of which hub it arrived on. So the log type has a
    real home and real coverage - just a shared, lower-parallelism one.

    With NO default hub, an undeclared log type has nowhere to go, and a
    detection for it can never see an event - so it stays an error. Per-namespace
    catch-alls are allowed, so any one makes an undeclared log type legitimate.
    """
    hubs = [s["hub"] for s in _sources() if s.get("default")]
    return hubs[0] if hubs else None


def validate(args):
    if not os.path.isdir(_bundle_dir()):
        print("validate: no bundle. Run `pyre pull` first."); return 1
    errors = 0
    declared = _declared_log_types()
    default_hub = _default_hub()
    # Log types with no hub of their own -> the RuleIDs that declare them. Not an
    # error when a catch-all exists, but still worth SEEING: it is the difference
    # between "covered on a dedicated, isolated hub" and "covered on the shared,
    # lowest-parallelism hub". Sets of RuleIDs, not counts, so a detection
    # declaring several undeclared log types is reported once rather than once
    # per log type.
    to_default: dict[str, set] = {}
    for path, meta in _iter_meta():
        atype = meta.get("AnalysisType", "rule")
        if atype != "rule":
            # globals/data-models/policies aren't streaming rules; just check the
            # helper's .py exists so a broken global reference is caught early.
            if atype == "global":
                py = os.path.join(os.path.dirname(path), os.path.basename(meta.get("Filename", "")))
                if meta.get("Filename") and not os.path.exists(py):
                    print(f"ERROR {path}: global Filename {meta.get('Filename')} not found"); errors += 1
            continue
        # A Panther "Simple Detection" expresses its logic as a YAML `Detection:`
        # block instead of a .py, so it has no Filename. pyre's Registry only
        # loads Python rules and skips anything without one - silently, which for
        # a detection engine is the worst kind of failure. Name it precisely
        # rather than reporting a confusing "missing Filename".
        if "Filename" not in meta and "Detection" in meta:
            print(f"ERROR {path}: '{meta.get('RuleID')}' is a Panther Simple Detection "
                  f"(YAML-only `Detection:` block, no Python). pyre runs Python rules only, "
                  f"so this would be SKIPPED and never evaluate anything. Port it to a "
                  f"rule(event) .py, or remove it from the bundle (dac.exclude)."); errors += 1
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
                if lt in declared:
                    continue
                if default_hub:
                    # Legitimate: Cribl sends it to the catch-all hub, the
                    # processor consumes that hub too, and routing is by the
                    # event's log-type field - so this detection is covered.
                    to_default.setdefault(lt, set()).add(meta.get("RuleID"))
                else:
                    print(f"ERROR {path}: LogTypes value '{lt}' has no matching entry in "
                          f"config/sources.yaml, and there is no `default: true` source to "
                          f"catch it (no Event Hub is sized for it)"); errors += 1

    if to_default:
        n = len(set().union(*to_default.values()))
        print(f"validate: {n} detection(s) across {len(to_default)} log type(s) have no "
              f"dedicated hub; they route to '{default_hub}' and are evaluated exactly the "
              f"same. Give a log type its own entry in config/sources.yaml if it needs "
              f"isolation or more parallelism than that hub's partition count.")
        if getattr(args, "show_default_routed", False):
            for lt, ids in sorted(to_default.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                print(f"    {lt:50} {len(ids)} detection(s)")
    print("validate: OK" if not errors else f"validate: {errors} error(s)")
    return 1 if errors else 0


def deps(args):
    """Fail if the bundle imports anything this environment can't resolve.

    The publish gate for the DaC-push-vs-deploy split: detections hot-reload in
    ~45s, but engine/requirements.txt is only installed when the Function App is
    DEPLOYED. So a DaC push can introduce an import the running engine doesn't
    have, and the detection just silently stops running. Run this in the publish
    pipeline with engine/requirements.txt installed (same Python version as the
    Function App) so a bundle that can't be imported never reaches a worker.
    """
    bundle = _bundle_dir()
    if not os.path.isdir(bundle):
        print("deps: no bundle. Run `pyre pull` first."); return 1
    sys.path.insert(0, os.path.join(REPO, "engine"))    # same layout run_local.py uses
    from pyre_engine.deps import scan_imports

    missing, unparseable = scan_imports(bundle)
    for path, err in unparseable:
        print(f"ERROR {os.path.relpath(path, REPO)}: does not parse - {err}")
    for name, users in sorted(missing.items()):
        print(f"\nERROR missing module '{name}' - imported by {len(users)} file(s) "
              f"the engine loads:")
        for py in users[:args.show]:
            print(f"    {os.path.relpath(py, REPO)}")
        if len(users) > args.show:
            print(f"    ... and {len(users) - args.show} more (--show N to list them)")

    if missing or unparseable:
        if missing:
            print(f"\ndeps: add the missing package(s) to engine/requirements.txt, then "
                  f"REDEPLOY the engine.\ndeps: publishing the bundle alone will NOT install "
                  f"them - those detections would load-fail and silently stop covering.")
        print(f"\ndeps: {len(missing)} missing module(s), {len(unparseable)} unparseable file(s)")
        return 1
    print(f"deps: OK - every import in the bundle resolves in this environment "
          f"(python {sys.version_info.major}.{sys.version_info.minor})")
    return 0


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
        if meta.get("AnalysisType", "rule") != "rule" or "RuleID" not in meta:
            continue          # `pyre validate` is what reports the missing RuleID
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


# enable/disable/status intentionally do not exist: a detection is enabled or
# disabled by its `Enabled` flag in the DaC .yml. Set it and `pyre publish`; the
# engine drops a disabled detection at load, so it is never evaluated (no
# separate control plane, no live flag store to query).
