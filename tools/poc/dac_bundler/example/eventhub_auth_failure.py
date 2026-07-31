"""Worked example: a detection over Azure Event Hubs RuntimeAuditLogs.

Only `rule()` is required. Everything else is optional and shapes the alert.

Note what the event looks like here: these are the fields of ONE RECORD, not the
`{"records": [...]}` envelope Azure wraps them in. The engine unwraps the
envelope before a detection ever sees it (EVENT_ENVELOPE_FIELD), so write
detections against the record's own fields.

Field names in Azure diagnostic logs vary by resource and category - Event Hubs
runtime audit records use PascalCase, other services use camelCase. Confirm the
real shape of YOUR records first (portal -> Event Hub -> Data Explorer -> View
events) rather than trusting this example's spelling.
"""


def rule(event):
    # `.get` returns None for a missing key rather than raising, so a record that
    # doesn't carry the field simply doesn't match.
    return event.get("ActivityStatus") == "Failure"


def title(event):
    return (f"Event Hub authorization failures from {event.get('ClientIp', 'unknown')} "
            f"on {event.get('EntityName', 'unknown entity')}")


def dedup(event):
    # Matches sharing this string are counted together against Threshold and
    # collapse into one alert. Per client IP here, so one noisy client can't
    # mask a second one.
    return f"eh-auth-failure:{event.get('ClientIp', 'unknown')}"


def severity(event):
    # Optional: override the YAML Severity per event.
    return "High" if event.get("AuthType") == "AAD" else "Medium"


def alert_context(event):
    # Whatever you return here is attached to the alert - the detail an analyst
    # needs without going back to the raw log.
    return {
        "clientIp": event.get("ClientIp"),
        "entityName": event.get("EntityName"),
        "authType": event.get("AuthType"),
        "activityName": event.get("ActivityName"),
        "resourceId": event.get("ResourceId"),
    }
