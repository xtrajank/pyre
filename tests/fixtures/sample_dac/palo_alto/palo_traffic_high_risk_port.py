HIGH_RISK_PORTS = {23, 2323, 3389, 445, 1433, 3306, 5900}


def rule(event):
    if event.get("action") != "allow":
        return False
    return int(event.get("dport", 0)) in HIGH_RISK_PORTS


def title(event):
    return f"Palo: allowed traffic to high-risk port {event.get('dport')} from {event.get('src_ip')}"


def dedup(event):
    return f"{event.get('src_ip')}:{event.get('dport')}"


def severity(event):
    return "HIGH" if int(event.get("dport", 0)) == 3389 else "MEDIUM"


def alert_context(event):
    return {
        "src_ip": event.get("src_ip"),
        "dst_ip": event.get("dst_ip"),
        "dport": event.get("dport"),
        "app": event.get("app"),
    }
