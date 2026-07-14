"""A stand-in global helper (Panther `AnalysisType: global`). Detections import it
by bare name — `from pyre_shared import is_high_risk_port` — from anywhere in the
DaC. Used by the tests to prove the engine puts helper dirs on the import path."""

HIGH_RISK_PORTS = {23, 445, 1433, 3306, 3389, 5900}


def is_high_risk_port(port) -> bool:
    try:
        return int(port) in HIGH_RISK_PORTS
    except (TypeError, ValueError):
        return False
