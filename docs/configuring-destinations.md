# Configuring destinations

Where signals and alerts go. This is the setting group you change when this
instance's job changes — from "write somewhere I can read it in the portal" to
"feed our SIEM" — and it is only settings. No code changes, no redeploy of
anything but the app settings themselves.

---

## Two streams, two configurations

Signals and alerts are consumed by different things, so they are configured
separately:

| | Volume | Deduplicated | Consumer |
|---|---|---|---|
| **Signal** | one per `rule()` that returned True | never | a lake or a blob you query later |
| **Alert** | one per threshold crossed, per dedup window | by definition | a case tool that opens a ticket |

Sending both to one place is normal, and costs exactly what one destination costs
— identical targets resolve to **one sink instance and one write per batch**.
Sending them to different places is two settings.

What each carries: [signals-and-alerts.md](signals-and-alerts.md).

---

## The three kinds

```
SIGNAL_DESTINATION = blob | http | none
ALERT_DESTINATION  = blob | http | none
```

**The selector alone decides.** Setting a URL never switches a mode, and a
selector pointed at something that isn't configured is a named entry in
`/health` → `problems` rather than a silent drop:

```
SIGNAL_DESTINATION=blob but SIGNAL_BLOB_ACCOUNT_URL is not set; signals will be dropped
```

### `blob`

Append blobs you can open and read straight from the portal's storage browser.
Newline-delimited JSON, one blob per stream per UTC day:

```
<container>/signals/2026-08-04.jsonl
<container>/alerts/2026-08-04.jsonl
```

```
SIGNAL_DESTINATION       = blob
SIGNAL_BLOB_ACCOUNT_URL  = https://<account>.blob.core.windows.net
SIGNAL_BLOB_CONTAINER    = pyre-output
```

The stream prefix lives **inside** the container, so pointing both streams at one
container keeps them separately readable and giving each its own container also
works — with no extra setting either way.

The app's identity needs **Storage Blob Data Contributor** on the account. An
append blob is the right primitive here: appending is one server-side operation
with no read-modify-write, so concurrent workers cannot clobber each other and
nothing already written is ever rewritten.

### `http`

POST to an endpoint — a SIEM's HTTP source, a lake's collector, a case tool's
webhook.

```
ALERT_DESTINATION        = http
ALERT_HTTP_URL           = https://siem.example/api/alerts
ALERT_HTTP_AUTH_HEADER   = @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/siem-token/)
ALERT_HTTP_BATCH         = false
HTTP_TIMEOUT_SECONDS     = 10
```

`*_HTTP_AUTH_HEADER` is sent whole as the `Authorization` header, so `Bearer x`,
`SharedKey y` and anything else all work without a setting per scheme. **It is
the one secret in this configuration surface — use a Key Vault reference.** For
that to resolve, the app's identity needs **Key Vault Secrets User** on the
vault.

`*_HTTP_BATCH` picks the shape the far end wants:

| | Sends | Default for |
|---|---|---|
| `true` | the whole batch as one JSON array | **signals** — a lake ingesting volume wants the array |
| `false` | one record per request | **alerts** — a case tool opens one ticket per request |

**Response status is checked.** A 401 or a 500 from the receiver is logged at
ERROR with the status and the number of records dropped. Swallowing it is how a
destination silently stops working for a week.

### `none`

Detections still run and state is still kept; the records are discarded. Both
streams set to `none` is legal but reported in `problems` — it is almost never
what you meant.

---

## Worked configurations

### A blob you can read in the portal

Everything in one storage account, nothing external. Good for standing an
instance up, and for proving a detection change before it reaches a real
destination.

```
SIGNAL_DESTINATION      = blob
SIGNAL_BLOB_ACCOUNT_URL = https://<account>.blob.core.windows.net
SIGNAL_BLOB_CONTAINER   = pyre-output
ALERT_DESTINATION       = blob
ALERT_BLOB_ACCOUNT_URL  = https://<account>.blob.core.windows.net
ALERT_BLOB_CONTAINER    = pyre-output
```

Both streams resolve to the same sink instance, so this is one blob client and
one write per batch.

### Signals to a lake, alerts to a case tool

The usual production shape: volume goes where volume is cheap, pages go where
people are.

```
SIGNAL_DESTINATION      = http
SIGNAL_HTTP_URL         = https://lake.example/collector/pyre
SIGNAL_HTTP_AUTH_HEADER = @Microsoft.KeyVault(SecretUri=.../lake-token/)
SIGNAL_HTTP_BATCH       = true

ALERT_DESTINATION       = http
ALERT_HTTP_URL          = https://case-tool.example/webhook
ALERT_HTTP_AUTH_HEADER  = @Microsoft.KeyVault(SecretUri=.../case-token/)
ALERT_HTTP_BATCH        = false
```

### Signals archived, alerts paged

Keep the full audit trail somewhere cheap and durable; page on what matters.

```
SIGNAL_DESTINATION      = blob
SIGNAL_BLOB_ACCOUNT_URL = https://<account>.blob.core.windows.net
SIGNAL_BLOB_CONTAINER   = pyre-signals

ALERT_DESTINATION       = http
ALERT_HTTP_URL          = https://case-tool.example/webhook
ALERT_HTTP_BATCH        = false
```

### Moving an instance from blob to HTTP

Change `*_DESTINATION` and add the URL. Nothing else — not the detections, not
`sources.yaml`, not the code. `/health` → `destinations` confirms what the
running app actually resolved:

```json
"destinations": {
  "signal": "blob https://acct.blob.core.windows.net/pyre-output",
  "alert":  "http https://case-tool.example/webhook (one record per request)"
}
```

---

## A write failure never fails the batch

This is deliberate and it has a cost worth knowing.

If a sink raised, the Event Hubs invocation would fail, the batch would be
**redelivered**, and the alert would fire twice. So every failure is logged and
swallowed instead:

```kusto
traces | where message contains "dropped"
```

That means an output failure is invisible in the destination — the only place it
appears is Application Insights. **Alert on that query.** See
[operations.md](operations.md#what-to-alert-on).

---

## Adding a kind of destination

One class in [`pyre_engine/sinks.py`](../pyre_engine/sinks.py) implementing
`write(records)` and `describe()`, plus one branch in `_target()`. Nothing above
that module changes — the processor only ever sees an object with `.write()`.

Two rules the new class must keep:

1. **It must not raise.** See above.
2. **`describe()` must be safe to log.** Use `redact()` on any URL — query
   strings on webhook URLs routinely carry a shared-access token, and both the
   startup lines and `/health` echo what `describe()` returns.
