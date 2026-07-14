"""Example unit test. Your existing detection validation modules plug in here;
CI runs `pyre test` which runs pytest over this folder. Detections come from the
offline fake-DaC fixture, not this repo."""
import importlib.util, os

BASE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_dac")


def _load(path):
    spec = importlib.util.spec_from_file_location("d", path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_palo_high_risk_port_matches_rdp():
    m = _load(os.path.join(BASE, "palo_alto", "palo_traffic_high_risk_port.py"))
    assert m.rule({"action": "allow", "dport": 3389}) is True
    assert m.severity({"dport": 3389}) == "HIGH"


def test_palo_ignores_allowed_https():
    m = _load(os.path.join(BASE, "palo_alto", "palo_traffic_high_risk_port.py"))
    assert m.rule({"action": "allow", "dport": 443}) is False


def test_cloudflare_sqli_detects_union_select():
    m = _load(os.path.join(BASE, "cloudflare", "cloudflare_http_sqli.py"))
    assert m.rule({"ClientRequestURI": "/x?id=1 UNION SELECT p FROM u"}) is True
