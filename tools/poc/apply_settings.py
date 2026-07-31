#!/usr/bin/env python3
"""Apply the repo's declarative app settings to a Function App, and report drift.

For "the repo is the sole contributor to this resource" to mean anything, the
app's CONFIGURATION has to come from the repo too - not just its code. A deploy
that ships repo-versioned code onto portal-edited settings is only half
reproducible, and the half that bites you at 3am is usually the settings.

So this reads config/appsettings/<env>.json, applies it, and then tells you about
anything on the app that the repo doesn't declare.

    python tools/poc/apply_settings.py --env poc -g <rg> -n pyre \\
        --var STORAGE_ACCOUNT=mystorage --var EVENTHUB_NAME=logs-in

    # see what would change, touch nothing
    python tools/poc/apply_settings.py --env poc -g <rg> -n pyre --dry-run --var ...

`${NAME}` placeholders are filled from --var or the environment, so secrets and
per-tenant values stay out of git.

DRIFT IS REPORTED, NOT DELETED. Deleting an undeclared setting is how you remove
a platform setting you didn't realise mattered and take the app down. Undeclared
settings are listed so a human decides: adopt it into the JSON, or remove it in
the portal. Pass --prune to delete them anyway, once you trust the list.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# Settings Azure or Terraform owns. Never applied, never reported as drift, never
# pruned - listing them in the repo would mean two owners for one value.
PLATFORM_PREFIXES = ("WEBSITE_", "FUNCTIONS_", "AzureWebJobs", "APPLICATIONINSIGHTS_",
                     "APPINSIGHTS_", "SCM_", "ENABLE_ORYX", "DOCKER_", "WEBSITES_")
# The Event Hub connection is wired by whoever created the namespace, and its
# value is a secret; the repo declares the hub NAME, not how to reach it.
PLATFORM_EXACT = {"EVENTHUB_CONNECTION", "AZURE_CLIENT_ID"}


def _is_platform(name: str) -> bool:
    return name in PLATFORM_EXACT or name.startswith(PLATFORM_PREFIXES)


def _az(args: list[str]) -> str:
    r = subprocess.run(["az", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"az {' '.join(args[:3])}... failed:\n{r.stderr.strip()}")
    return r.stdout


def _substitute(value: str, variables: dict) -> str:
    """Fill ${NAME} from --var or the environment. An unresolved placeholder is a
    hard error: silently deploying the literal string '${STORAGE_ACCOUNT}' would
    produce an app that starts fine and fails on its first blob call."""
    missing = []

    def repl(m):
        key = m.group(1)
        if key in variables:
            return variables[key]
        if key in os.environ:
            return os.environ[key]
        missing.append(key)
        return m.group(0)

    out = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)
    if missing:
        raise KeyError(", ".join(sorted(set(missing))))
    return out


def load_desired(env: str, variables: dict) -> dict:
    path = os.path.join(REPO, "config", "appsettings", f"{env}.json")
    if not os.path.exists(path):
        sys.exit(f"apply-settings: no such file: {path}")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    desired, unresolved = {}, {}
    for key, value in raw.items():
        if key.startswith("_"):          # _comment and friends
            continue
        if _is_platform(key):
            print(f"  ! ignoring platform-owned key in {env}.json: {key}")
            continue
        try:
            desired[key] = _substitute(str(value), variables)
        except KeyError as e:
            unresolved[key] = str(e)
    if unresolved:
        print("apply-settings: unresolved ${...} placeholders:", file=sys.stderr)
        for k, v in unresolved.items():
            print(f"  {k}: needs {v}", file=sys.stderr)
        sys.exit("Pass them with --var NAME=value, or set them in the environment.")
    return desired


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="config/appsettings/<env>.json")
    ap.add_argument("-g", "--resource-group", required=True)
    ap.add_argument("-n", "--name", required=True, help="function app name")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                    help="fill a ${NAME} placeholder (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--prune", action="store_true",
                    help="also DELETE settings the repo doesn't declare")
    args = ap.parse_args()

    variables = dict(v.split("=", 1) for v in args.var if "=" in v)
    desired = load_desired(args.env, variables)

    current_list = json.loads(_az(["functionapp", "config", "appsettings", "list",
                                   "-g", args.resource_group, "-n", args.name, "-o", "json"]))
    current = {s["name"]: s.get("value") or "" for s in current_list}

    to_set = {k: v for k, v in desired.items() if current.get(k) != v}
    undeclared = sorted(k for k in current
                        if k not in desired and not _is_platform(k))

    print(f"\n{args.name} ({args.env}): {len(desired)} setting(s) declared in the repo")

    if to_set:
        print(f"\n  {len(to_set)} to change:")
        for k, v in sorted(to_set.items()):
            was = current.get(k)
            shown = "(absent)" if was is None else (f"'{was}'" if len(str(was)) < 60 else "'…'")
            print(f"    {k}\n        {shown} -> '{v}'")
    else:
        print("\n  in sync - nothing to change")

    if undeclared:
        print(f"\n  {len(undeclared)} setting(s) on the app that the repo does NOT declare:")
        for k in undeclared:
            print(f"    {k}")
        print("\n  These are drift. Either add them to "
              f"config/appsettings/{args.env}.json (if they should be repo-owned)")
        print("  or remove them from the app. Re-run with --prune to delete them here.")

    if args.dry_run:
        print("\n(dry run - nothing changed)")
        return 0

    if to_set:
        pairs = [f"{k}={v}" for k, v in to_set.items()]
        _az(["functionapp", "config", "appsettings", "set", "-g", args.resource_group,
             "-n", args.name, "--settings", *pairs, "-o", "none"])
        print(f"\napplied {len(to_set)} setting(s)")

    if undeclared and args.prune:
        _az(["functionapp", "config", "appsettings", "delete", "-g", args.resource_group,
             "-n", args.name, "--setting-names", *undeclared, "-o", "none"])
        print(f"pruned {len(undeclared)} undeclared setting(s)")

    if to_set or (undeclared and args.prune):
        print("the app restarts automatically after a settings change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
