"""pyre CLI: manage detections and deployment from the DaC repo.

Disable a detection by setting `Enabled: false` in its .yml and running
`pyre publish` - the engine drops a disabled detection at load, live within
refresh_interval_seconds, no redeploy. "deploy" publishes the Function App and
the detection bundle. Infra itself is managed by Terraform (see infra/), not here.
"""
import argparse
from . import commands


def main(argv):
    p = argparse.ArgumentParser(prog="pyre")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pull", help="clone the external DaC repo (config/detections.yaml) into the local bundle")

    v = sub.add_parser("validate", help="lint YAML, check LogTypes + Filename resolve")
    v.add_argument("--show-default-routed", action="store_true",
                   help="list the log types that have no dedicated hub and will route to "
                        "the catch-all (config/sources.yaml `default: true`)")

    dp = sub.add_parser("deps", help="check every import in the bundle resolves in THIS "
                                     "environment (run with engine/requirements.txt installed)")
    dp.add_argument("--show", type=int, default=5, metavar="N",
                    help="max files to list per missing module (default 5)")

    t = sub.add_parser("test", help="run detection unit tests")
    t.add_argument("detection_id", nargs="?", default=None)

    sub.add_parser("build", help="build the LogType->detections index + bundle")

    pub = sub.add_parser("publish", help="upload the bundle to Blob + flip the pointer (workers hot-reload)")
    pub.add_argument("--account-url", default=None, help="blob account url (else config / BUNDLE_BLOB_ACCOUNT_URL)")
    pub.add_argument("--container", default=None, help="blob container (else config bundle.blob.container)")

    d = sub.add_parser("deploy", help="publish Function App + upload detection bundle")
    d.add_argument("--env", required=True)

    args = p.parse_args(argv)
    return getattr(commands, args.cmd)(args)
