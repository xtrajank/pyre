SQLI_MARKERS = ("union select", "' or '1'='1", "sleep(", "information_schema", "--")


def rule(event):
    uri = (event.get("ClientRequestURI") or "").lower()
    return any(m in uri for m in SQLI_MARKERS)


def title(event):
    return f"Cloudflare: possible SQLi from {event.get('ClientIP')}"


def dedup(event):
    return event.get("ClientIP", "unknown")


def alert_context(event):
    return {
        "client_ip": event.get("ClientIP"),
        "host": event.get("ClientRequestHost"),
        "uri": event.get("ClientRequestURI"),
        "ua": event.get("ClientRequestUserAgent"),
    }
