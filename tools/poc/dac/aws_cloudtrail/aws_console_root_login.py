from poc_helpers import aws_account, source_ip


def rule(event):
    return (
        event.get("eventName") == "ConsoleLogin"
        and event.deep_get("userIdentity", "type") == "Root"
        and event.deep_get("responseElements", "ConsoleLogin") == "Success"
    )


def title(event):
    return f"AWS root console login from {source_ip(event)} in account [{aws_account(event)}]"


def dedup(event):
    # Group every root login in an account into ONE alert per dedup window.
    # Two root logins from different IPs therefore write two SIGNALS but raise a
    # single ALERT - the behaviour the POC demo is meant to show.
    return f"root-login:{aws_account(event)}"


def alert_context(event):
    return {
        "sourceIPAddress": source_ip(event),
        "userIdentityArn": event.deep_get("userIdentity", "arn"),
        "eventTime": event.get("eventTime"),
        "mfaUsed": event.deep_get("additionalEventData", "MFAUsed"),
    }
