"""Does this bundle's code actually import in this engine's environment?

THE PROBLEM THIS EXISTS TO SOLVE. A detection's Python is hot-reloaded from the
DaC repo within refresh_interval_seconds of a push. The engine's Python
ENVIRONMENT is not: third-party packages are installed from engine/requirements.txt
at DEPLOY time and baked into the deployment package. The two lifecycles are
completely decoupled, so a DaC push that adds `import dateutil` goes live in ~45s
into an interpreter that may not have dateutil - and the only symptom is a
"skipping detection ..." warning in Log Analytics while that detection silently
stops covering anything.

So the publish pipeline runs this against an environment built from
engine/requirements.txt and REFUSES to publish a bundle it can't import. A
detection that can't run is worse than a failed pipeline: the pipeline is loud.

Why static (ast) and not just importing the modules:
  * Importing runs arbitrary DaC code in CI. This only parses it.
  * It's fast - no module execution, no import side effects.
  * It reports EVERY missing module in one pass instead of dying on the first.

Import name vs package name (dateutil -> python-dateutil, yaml -> pyyaml) is
deliberately not modelled here: resolution is done with importlib against the
real environment, so the mapping is whatever pip actually installed. That means
the check is only as truthful as CI's Python version and requirements matching
the Function App's - keep both at the runtime_version in infra/modules/function_app.
"""
import ast
import importlib.util
import os
import sys

from .registry import _prepare_imports, _scan


def _imported_names(py_path: str) -> tuple[set[str], str | None]:
    """Top-level module names imported by a file, without executing it.
    Returns (names, syntax_error_or_None).

    Walks the WHOLE tree, not just module-level statements: an import inside a
    rule() body fails per-event at detection time, which is worse than failing at
    load time, so it counts just the same.
    """
    try:
        with open(py_path, "rb") as fh:          # ast.parse handles the decode
            tree = ast.parse(fh.read(), filename=py_path)
    except SyntaxError as e:
        return set(), f"{type(e).__name__}: {e}"
    except OSError as e:
        return set(), f"unreadable: {e}"

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import (`from . import x`) - resolved inside
            # the bundle, never a pip package.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names, None


def _resolvable(name: str) -> bool:
    """Can this interpreter import `name`? find_spec on a TOP-LEVEL name locates
    without executing (unlike find_spec("a.b"), which would import "a")."""
    if name in sys.stdlib_module_names:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def scan_imports(bundle_dir: str) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Check every .py the engine could load.

    Returns ({missing module: [files that import it]}, [(file, parse error)]).

    Only rule and global-helper files are checked - exactly what Registry loads.
    The DaC's own *_tests.py are ignored because the engine never imports them
    (they'd otherwise demand unittest-only deps the engine has no use for).
    Bundle-internal helper imports resolve because _prepare_imports puts the same
    dirs on sys.path that the engine will.
    """
    scan = _scan(bundle_dir)
    _prepare_imports(bundle_dir, scan.helper_dirs, scan.helper_modules)

    missing: dict[str, list[str]] = {}
    unparseable: list[tuple[str, str]] = []
    resolved: dict[str, bool] = {}               # find_spec hits the filesystem
    for py in [p for _meta, p in scan.rules] + sorted(scan.helper_files):
        names, err = _imported_names(py)
        if err:
            unparseable.append((py, err))
            continue
        for name in names:
            ok = resolved.get(name)
            if ok is None:
                ok = resolved[name] = _resolvable(name)
            if not ok:
                missing.setdefault(name, []).append(py)
    return missing, unparseable
