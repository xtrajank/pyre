"""Shared helpers for the POC detections.

Panther-style global helper: a plain module, paired with a `AnalysisType: global`
YAML, that detections import BY BARE NAME (`from poc_helpers import ...`). The
engine puts this directory on sys.path before loading any detection, so the
import resolves the same way it does in panther-analysis.
"""


def aws_account(event) -> str:
    """The account the event landed in, however CloudTrail spelled it."""
    return event.get("recipientAccountId") or event.deep_get(
        "userIdentity", "accountId", default="unknown"
    )


def actor(event) -> str:
    """A human-readable "who did this", falling back through CloudTrail's
    several identity shapes so a title is never blank."""
    return (
        event.deep_get("userIdentity", "userName")
        or event.deep_get("userIdentity", "sessionContext", "sessionIssuer", "userName")
        or event.deep_get("userIdentity", "arn")
        or "unknown"
    )


def source_ip(event) -> str:
    return event.get("sourceIPAddress", "unknown")
