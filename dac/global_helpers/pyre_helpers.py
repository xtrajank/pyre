"""Helpers shared across detections in this repo.

Import these by bare name from any detection, at any depth:

    from pyre_helpers import internal_ip

That works because this folder ships a paired `.yml` with
`AnalysisType: global`, which is what puts the folder on the import path.
"""
import ipaddress

# RFC1918 plus loopback and link-local. Deliberately NOT ipaddress.is_private,
# which also reports the IANA documentation ranges (192.0.2.0/24, 198.51.100.0/24,
# 203.0.113.0/24) as private - so any test using those addresses silently stops
# matching.
_INTERNAL = [ipaddress.ip_network(n) for n in
             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
              "127.0.0.0/8", "169.254.0.0/16", "fc00::/7", "::1/128")]


def internal_ip(addr: str | None) -> bool:
    """True for an address inside the corporate network. Anything unparseable
    counts as external, which is the safe direction for a detection."""
    try:
        ip = ipaddress.ip_address(addr)
    except (TypeError, ValueError):
        return False
    return any(ip in net for net in _INTERNAL)
