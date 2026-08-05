# Signals and alerts

The two records pyre emits, and every field on them. This is the contract a
destination consumes — if you are writing the far end of `SIGNAL_HTTP_URL` or
querying `pyre-output`, this is the page.

The schema is defined in exactly one place,
[`pyre_engine/records.py`](../pyre_engine/records.py), and asserted field by
field in `tests/test_engine.py`.

---

## The difference

| | Signal | Alert |
|---|---|---|
| Written when | **every** `rule()` returns True | a match also clears `Threshold:` and no alert is already open for its dedup string |
| Deduplicated | never — repeats are real | by definition, once per dedup string per `DedupPeriodMinutes:` |
| Volume | high | low |
| Purpose | the audit of what matched | the page that opens a case |

**Expect far more signals than alerts.** That gap is thresholds and dedup doing
their job, not events going missing.

### How they link

```
p_record_type   "signal" | "alert"
p_signal_id     unique per match              (signals)
p_alert_id      unique per alert              (alerts; on a signal, the alert
                                               this match RAISED or JOINED)
```

On a signal `p_alert_id` is `null` until a match actually reaches an alert. So:

| Query | Answers |
|---|---|
| signals where `p_alert_id = <id>` | which matches make up this alert |
| signals where `p_alert_id is null` | what matched but was held back by a threshold, or came from a `CreateAlert: false` detection |
| alerts where `p_dedup = <x>` | the one alert covering that dedup group in its window |

`p_first_signal_id` on an alert names the exact match that raised it.

---

## Field prefixes

**Every engine-added field is `p_`-prefixed, and the raw log record lives
untouched under `p_event`.** An event carrying its own `severity` or `title` can
never collide with the engine's.

`p_any_*` fields are pivot values a detection declared through `indicators()` —
see [below](#p_any_-pivot-fields).

---

## Signal

```json
{
  "p_record_type": "signal",
  "p_signal_id": "2c319cfb-3baf-4000-b2e3-dabf742ccfb6",
  "p_alert_id": null,

  "p_detection_id": "Azure.EventHub.AuthFailure",
  "p_detection_name": "Repeated Event Hub Authorization Failures",
  "p_severity": "Medium",
  "p_tags": ["Azure", "EventHub"],
  "p_reports": { "MITRE ATT&CK": ["TA0006:T1110"] },

  "p_log_type": "RuntimeAuditLogs",
  "p_source_namespace": "platform",
  "p_source_hub": "applog",

  "p_dedup": "eh-auth-failure:203.0.113.55",
  "p_event_time": "2026-07-31T14:00:00.1234567Z",
  "p_processed_time": "2026-08-04T03:20:58.801Z",

  "p_any_ip_addresses": ["203.0.113.55"],
  "p_any_actor_ids": ["RootManageSharedAccessKey"],

  "p_event": { "...": "the raw log record, exactly as it arrived" }
}
```

| Field | |
|---|---|
| `p_signal_id` | Unique per match. |
| `p_alert_id` | The alert this match raised or joined, else `null`. |
| `p_detection_id` | The detection's `RuleID`. |
| `p_detection_name` | Its `DisplayName`, falling back to the `RuleID`. |
| `p_severity` | The detection's `severity(event)` for **this** event, falling back to the YAML `Severity`. |
| `p_tags`, `p_reports` | Straight from the YAML. `[]` / `{}` when absent. |
| `p_log_type` | The routing value that selected this detection. |
| `p_source_namespace`, `p_source_hub` | Which feed produced it — what makes one destination readable when twenty sources write into it. |
| `p_dedup` | The dedup group. `dedup(event)`, falling back to `title(event)`, truncated at 1000 characters. |
| `p_event_time` | From the record, read out of the source's `event_time_field`. **The producer's clock** — it may be missing, wrong, or in any format. `""` when the field isn't there. |
| `p_processed_time` | When the engine evaluated it. ISO 8601 UTC, always present, always trustworthy. Use this for latency; use `p_event_time` for what happened when. |
| `p_event` | The raw record, unmodified. Not the `{"records": [...]}` envelope — the record inside it. |

---

## Alert

Deliberately **self-sufficient**: everything a case tool needs to open a ticket,
without joining back to the signals stream.

```json
{
  "p_record_type": "alert",
  "p_alert_id": "c0716ab0-ab01-4474-aa5a-8ab6af1bfcd6",

  "p_detection_id": "Azure.EventHub.AuthFailure",
  "p_detection_name": "Repeated Event Hub Authorization Failures",
  "p_severity": "Medium",
  "p_title": "Event Hub authorization failures from 203.0.113.55 on logs-in",
  "p_description": "Three or more failed Event Hubs authorization attempts from one client.",
  "p_runbook": "Check whether the client IP and identity are expected senders...",
  "p_reference": "https://learn.microsoft.com/azure/event-hubs/monitor-event-hubs-reference",
  "p_tags": ["Azure", "EventHub"],
  "p_reports": { "MITRE ATT&CK": ["TA0006:T1110"] },

  "p_log_type": "RuntimeAuditLogs",
  "p_source_namespace": "platform",
  "p_source_hub": "applog",

  "p_dedup": "eh-auth-failure:203.0.113.55",
  "p_threshold": 3,
  "p_dedup_period_minutes": 60,
  "p_signal_count": 3,

  "p_first_signal_id": "35839932-a96c-40de-b4f5-70199c6da77a",
  "p_first_event_time": "2026-07-31T14:00:03.4567890Z",
  "p_created_time": "2026-08-04T03:20:58.801Z",

  "p_context": { "clientIp": "203.0.113.55", "entityName": "logs-in" },

  "p_any_ip_addresses": ["203.0.113.55"],
  "p_any_actor_ids": ["RootManageSharedAccessKey"],

  "p_event": { "...": "the event that raised it" }
}
```

| Field | |
|---|---|
| `p_title` | `title(event)` for the event that raised it, falling back to the `RuleID`. The one-line summary. |
| `p_description`, `p_runbook`, `p_reference` | From the YAML. **What to do about it, delivered with the page** rather than a link back to the detection repo. `""` when absent. |
| `p_threshold` | The `Threshold:` this alert crossed. |
| `p_dedup_period_minutes` | How long this alert covers repeats. Nothing new will alert for this `p_dedup` until it elapses. |
| `p_signal_count` | The counter's value when the alert was raised. **See the caveat below.** |
| `p_first_signal_id` | The signal for the match that raised it. |
| `p_first_event_time` | That event's own timestamp. |
| `p_created_time` | When the engine raised the alert. ISO 8601 UTC. |
| `p_context` | Whatever `alert_context(event)` returned. `{}` when the detection doesn't define one. |
| `p_event` | The event that raised it — one concrete example, so triage doesn't require a second query. |

> ### `p_signal_count` is point-in-time
>
> Alerts are written once and never rewritten, so later matches joining this
> alert's dedup window **do not update it**. For the live total, count signals
> filtered on `p_alert_id`. With `Threshold: 3`, `p_signal_count` is 3 on
> essentially every alert — its value is telling you the threshold was met, not
> how big the incident got.

**An alert carries no `p_signal_id`.** The signals stream is where per-match
detail lives; `p_alert_id` is the join.

---

## `p_any_*` pivot fields

The answer to "everything involving this IP" across log types that spell the
field differently. They appear on both records, and only when the detection
declares them:

```python
def indicators(event):
    return {
        "ip_addresses": [event.get("ClientIp")],
        "usernames":    [event.get("UserPrincipalName")],
        "actor_ids":    [event.get("AuthKey")],
    }
```

Both `ip_addresses` and `p_any_ip_addresses` are accepted; the prefix is added if
missing. Values are coerced to a **sorted list of strings** with empties dropped,
so the field is queryable regardless of what the detection handed back, and a
detection returning nonsense costs its own indicators rather than the record.

There is no fixed vocabulary — pick names your destination can index.
`ip_addresses`, `usernames`, `actor_ids`, `domain_names`, `hostnames`,
`sha256_hashes`, `emails`, `trace_ids` are conventional.

**Why declared, not extracted:** only the detection knows which of its log type's
fields are an actor and which are a hostname. A regex sweep over every event
would cost far more per event and still guess wrong.

---

## Reading the blob output

```
pyre-output/signals/2026-08-04.jsonl
pyre-output/alerts/2026-08-04.jsonl
```

Newline-delimited JSON — one record per line, appended, never rewritten. Open
either in the portal's storage browser, or:

```powershell
python tools/run_local.py --json | ConvertFrom-Json
```

which produces the identical shape from your laptop with no Azure at all.
