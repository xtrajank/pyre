from poc_helpers import actor, aws_account


def rule(event):
    return event.get("eventName") == "CreateUser" and not event.get("errorCode")


def _new_user(event):
    return event.deep_get("requestParameters", "userName", default="unknown")


def title(event):
    return f"IAM user [{_new_user(event)}] created by {actor(event)} in account [{aws_account(event)}]"


def dedup(event):
    return f"iam-user-created:{aws_account(event)}:{_new_user(event)}"


def alert_context(event):
    return {
        "newUser": _new_user(event),
        "createdBy": actor(event),
        "eventTime": event.get("eventTime"),
    }
