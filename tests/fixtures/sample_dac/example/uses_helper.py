"""A detection that imports a GLOBAL HELPER by bare name — the pattern real DaCs
(panther-analysis) use everywhere. This import must resolve at load time, which is
what tests/test_registry_loader.py::test_global_helper_is_importable checks."""
from pyre_shared import is_high_risk_port


def rule(event):
    return is_high_risk_port(event.get("dport", 0))


def title(event):
    return f"Helper-backed detection: high-risk port {event.get('dport')}"
