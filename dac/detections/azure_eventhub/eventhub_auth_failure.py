"""A worked detection over Azure Event Hubs RuntimeAuditLogs.

Only `rule()` is required. Everything else here is optional and shapes the alert.

Note what `event` is: the fields of ONE RECORD, not the `{"records": [...]}`
envelope Azure wraps them in. The engine unwraps the envelope before a detection
ever sees it, so write against the record's own fields.

Field names in Azure diagnostic logs vary by resource and category - Event Hubs
runtime audit records use PascalCase, other services use camelCase. Confirm the
real shape of YOUR records (portal -> Event Hub -> Data Explorer -> View events)
rather than trusting this example's spelling.
"""
from pyre_helpers import internal_ip


def rule(event):
    # `.get` returns None for a missing key rather than raising, so a record
    # that doesn't carry the field simply doesn't match.
    if event.get("ActivityStatus") != "Failure":
        return False
    # Failures from inside the network are almost always a misconfigured client.
    return not internal_ip(event.get("ClientIp"))


def title(event):
    return (f"Event Hub authorization failures from {event.get('ClientIp', 'unknown')} "
            f"on {event.get('EntityName', 'unknown entity')}")


def dedup(event):
    # Matches sharing this string count together against Threshold and collapse
    # into one alert. Per client IP here, so one noisy client can't mask another.
    return f"eh-auth-failure:{event.get('ClientIp', 'unknown')}"


def severity(event):
    # Optional: override the YAML Severity per event.
    return "High" if event.get("AuthType") == "AAD" else "Medium"


def alert_context(event):
    # Whatever you return is attached to the alert - the detail an analyst needs
    # without going back to the raw log.
    return {
        "clientIp": event.get("ClientIp"),
        "entityName": event.get("EntityName"),
        "authType": event.get("AuthType"),
        "activityName": event.get("ActivityName"),
        "resourceId": event.get("ResourceId"),
    }


def indicators(event):
    # Optional. These become `p_any_*` fields on the signal AND the alert, which
    # is how "everything involving this IP" works across log types that spell
    # the field differently. Only this detection knows which of its fields are
    # pivots, which is why it declares them rather than the engine guessing.
    return {
        "ip_addresses": [event.get("ClientIp")],
        "actor_ids": [event.get("AuthKey")],
    }
