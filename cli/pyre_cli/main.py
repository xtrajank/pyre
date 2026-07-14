"""pyre CLI: manage detections and deployment from the DaC repo.

The unit you enable/disable is a DETECTION, not infrastructure. "disable" flips
a flag (App Configuration) that the engine reads with a short cache, so it takes
effect in seconds with no redeploy. "deploy" publishes the Function App and the
detection bundle. Infra itself is managed by Terraform (see infra/), not here.
"""
import argparse
from . import commands


def main(argv):
    p = argparse.ArgumentParser(prog="pyre")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pull", help="clone the external DaC repo (config/detections.yaml) into the local bundle")

    sub.add_parser("validate", help="lint YAML, check LogTypes + Filename resolve")

    t = sub.add_parser("test", help="run detection unit tests")
    t.add_argument("detection_id", nargs="?", default=None)

    sub.add_parser("build", help="build the LogType->detections index + bundle")

    pub = sub.add_parser("publish", help="upload the bundle to Blob + flip the pointer (workers hot-reload)")
    pub.add_argument("--account-url", default=None, help="blob account url (else config / BUNDLE_BLOB_ACCOUNT_URL)")
    pub.add_argument("--container", default=None, help="blob container (else config bundle.blob.container)")

    d = sub.add_parser("deploy", help="publish Function App + upload detection bundle")
    d.add_argument("--env", required=True)

    for name in ("enable", "disable"):
        s = sub.add_parser(name, help=f"{name} a detection at runtime (App Config flag)")
        s.add_argument("detection_id")
        s.add_argument("--env", required=True)

    st = sub.add_parser("status", help="show which detections are live per env")
    st.add_argument("--env", required=True)

    args = p.parse_args(argv)
    return getattr(commands, args.cmd)(args)
