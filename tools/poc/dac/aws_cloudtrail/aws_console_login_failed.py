from poc_helpers import actor, aws_account, source_ip


def rule(event):
    return (
        event.get("eventName") == "ConsoleLogin"
        and event.deep_get("responseElements", "ConsoleLogin") == "Failure"
    )


def title(event):
    return f"Repeated failed AWS console logins for {actor(event)} in account [{aws_account(event)}]"


def dedup(event):
    # The threshold counts matches sharing THIS string, so failures are counted
    # per user - three failures by one user alerts; one failure each by three
    # different users does not.
    return f"login-failure:{aws_account(event)}:{actor(event)}"


def severity(event):
    # Dynamic severity: a root account being brute-forced outranks a normal user.
    return "High" if event.deep_get("userIdentity", "type") == "Root" else "Medium"


def alert_context(event):
    return {
        "user": actor(event),
        "sourceIPAddress": source_ip(event),
        "eventTime": event.get("eventTime"),
    }
