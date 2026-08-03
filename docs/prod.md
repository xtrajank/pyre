# prod — all log sources, shared state, a real SIEM

Everything here assumes you have run [poc.md](poc.md) and have a
[dev](dev.md) environment. Prod differs from dev in exactly four ways, and each
one is a setting:

| | dev | prod |
|---|---|---|
| Log sources | one or two | all of them — [more entries in `sources.yaml`](#1-every-log-source) |
| Dedup state | in-process, per worker | shared — [`REDIS_HOST`](#2-shared-state) |
| Output | blobs you read in the portal | your SIEM — [`OUTPUT_HTTP_URL`](#3-where-signals-and-alerts-go) |
| Deploys | from VS Code | from a pipeline, behind an approval |

No engine code changes between them. That is the point of the split.

---

## Resources

| Resource | Sizing | Why |
|---|---|---|
| Event Hubs namespace | Standard. One hub per high-volume source, one shared hub for the long tail. Partitions = your parallelism ceiling: 32 for a firewall firehose, 2–8 for the rest. | Partitions cannot be reduced later, only increased. Size the loud ones generously. |
| Function App | Linux, Python 3.11. **Premium (EP1)** or Flex Consumption. | Consumption cold-starts and caps out; a detection engine should stay warm. |
| Storage account | Standard LRS. Containers `detections`, `pyre-output`. | |
| Azure Cache for Redis | Basic C0 is enough to start; Standard C1 for HA. | Shared dedup/threshold state. See below. |
| Application Insights | | Where the engine's logs and metrics go. |
| Log Analytics workspace | | Backs App Insights; query the logs with KQL. |

Everything is reached by **managed identity**. Role assignments the Function
App's identity needs:

| Scope | Role |
|---|---|
| Storage account | Storage Blob Data Contributor |
| Event Hubs namespace | Azure Event Hubs Data Receiver |
| Redis | Redis Data Owner (Data Access Configuration → add the identity) |

No connection strings, no keys, no secrets in app settings.

---

## 1. Every log source

Each entry in [config/sources.yaml](../config/sources.yaml) becomes its own
function, its own checkpoint, and its own scaling unit. There is no limit and no
code to write.

```yaml
sources:
  # Azure diagnostic logs: the defaults already fit.
  - hub: insights-logs-signinlogs
  - hub: insights-logs-auditlogs

  # A high-volume feed on its own hub, in its own namespace.
  - hub: palo-traffic-in
    connection: EVENTHUB_CONNECTION_NETWORK
    log_type_field: dataset          # what your normalizer stamps
    event_time_field: _time
    envelope_field: ""               # one message, one record

  # A feed where a second consumer already reads $Default.
  - hub: cloudflare-in
    consumer_group: pyre
```

Rules worth knowing before you add twenty of them:

- **`connection:` names an app setting, not a value.** Each namespace gets its
  own setting; hubs in the same namespace share one.
- **Two functions on one hub need different consumer groups.** If anything else
  already consumes a hub, create a consumer group for pyre and name it.
- **`log_type_field` is per source because feeds genuinely differ.** An Azure
  diagnostic feed routes on `category`; a normalized feed routes on whatever the
  normalizer stamps. Check each one in Data Explorer before you add it.
- **Adding a source is a deploy** — the triggers are registered at startup. It's a
  config-only change, but it does go through the pipeline.
- **A hub that doesn't exist fails the whole app at startup.** Add the entry and
  the hub together, and watch the function list after deploying.

### Onboarding a source, in order

1. Confirm the hub exists and is receiving (Data Explorer → View events).
2. Note the routing field, its values, and the timestamp field. Add the entry to
   `sources.yaml`.
3. Publish detections whose `LogTypes:` contain those exact values.
4. Deploy. Check `/health`: the source appears, and its values are in
   `log_types`.
5. Watch **Log stream** for `no detections are registered for these log-type
   values` — that line names anything arriving with no coverage.

---

## 2. Shared state

Set **`REDIS_HOST`** to your cache's hostname. That single setting is the whole
change.

Without it, dedup counters, thresholds, `unique()` sets, the storm limiter and
the redelivery guard live inside one worker process. On one instance that is
correct. Across scale-out it is not: two workers count independently, so a
`Threshold: 5` can fire twice at 5 matches each, and one alert can be raised
twice.

With it, all of that is atomic and shared, and the behaviour is identical no
matter how many instances Azure runs.

| Setting | Value |
|---|---|
| `REDIS_HOST` | `<name>.redis.cache.windows.net` |
| `REDIS_PORT` | `6380` (default; TLS) |

Auth is Entra via the Function App's managed identity — the engine fetches a
fresh token per connection, so nothing expires under a long-running worker.
Assign **Redis Data Owner** in the cache's **Data Access Configuration**.

Verify: `/health` reports `"state": "redis"`.

---

## 3. Where signals and alerts go

| Setting | Effect |
|---|---|
| `OUTPUT_HTTP_URL` | Every signal and alert is POSTed as a JSON array to this endpoint — your SIEM/lake HTTP source. Takes precedence over the blob. |
| `OUTPUT_BLOB_ACCOUNT_URL` | Append-blob output. Keep it set in prod too: it costs nothing when `OUTPUT_HTTP_URL` wins, and it's the setting you fall back to if the SIEM endpoint breaks. |
| `ALERT_WEBHOOK_URL` | Optional. Each **alert** is additionally POSTed on its own to a case tool. Signals never are. |

Every record carries `p_record_type` (`signal` or `alert`), so one endpoint can
receive both and split them. Route `signal` to cheap long-term storage and
`alert` to whatever a human watches.

A sink that fails **never fails the batch** — Event Hubs would redeliver it and
the alert would fire twice. Failures land in Application Insights instead, which
means **you must alert on them**; see [monitoring](#monitoring).

---

## 4. Full app settings

| Setting | prod value |
|---|---|
| `PYRE_ENV` | `prod` |
| `EVENTHUB_CONNECTION` (+ one per extra namespace) | identity-based: `EVENTHUB_CONNECTION__fullyQualifiedNamespace` = `<ns>.servicebus.windows.net`, `EVENTHUB_CONNECTION__credential` = `managedidentity` |
| `DAC_BLOB_ACCOUNT_URL` | `https://<prod-storage>.blob.core.windows.net` |
| `DAC_CONTAINER` | `detections` |
| `DAC_REFRESH_SECONDS` | `60` |
| `OUTPUT_HTTP_URL` | your SIEM's HTTP source |
| `OUTPUT_BLOB_ACCOUNT_URL` | `https://<prod-storage>.blob.core.windows.net` |
| `ALERT_WEBHOOK_URL` | your case tool, if you have one |
| `REDIS_HOST` | `<name>.redis.cache.windows.net` |
| `STORM_LIMIT` | `1000` |
| `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` | `256` |
| `WEBSITE_RUN_FROM_PACKAGE` | `1` (set by the deploy; don't fight it) |

`maxEventBatchSize` is the main cost lever: one invocation evaluates the whole
batch, so 256 costs roughly a quarter of what 64 does at the same volume. It's a
ceiling, not a wait — a small backlog still delivers immediately.

---

## 5. Deploying

Two pipelines, both gated on an approval you configure once (Pipelines →
Environments → `prod` → Approvals and checks):

| Pipeline | Triggered by | Does |
|---|---|---|
| [azure-pipelines.yml](../azure-pipelines.yml) | push to `main` of this repo | run tests → deploy to dev → **approval** → deploy to prod |
| [dac/azure-pipelines.yml](../dac/azure-pipelines.yml) | push to `main` of the detections repo | validate → publish to dev → **approval** → publish to prod |

Set the service connection names and app/account names at the top of each. Each
service connection's identity needs Contributor on the Function App, or Storage
Blob Data Contributor on the storage account, respectively.

Engine deploys restart the app and cost a cold start. **Detection publishes do
not** — that's why they're separate pipelines, and why the second one runs many
times a week while the first runs rarely.

> The build agent's outbound IPs are not your laptop's. If the Function App
> restricts inbound access, allow the agent's range or use a self-hosted agent.
> See [troubleshooting](troubleshooting.md#deploy-fails-with-403-or-failed-to-fetch).

**Rollback.** Engine: re-run the previous pipeline run. Detections: re-upload a
`current.json` naming an older bundle zip — they're all still in the container,
and a worker picks it up within `DAC_REFRESH_SECONDS`. That rollback takes about
fifteen seconds and needs no pipeline at all.

---

## Monitoring

Application Insights → **Logs**. The queries that matter:

```kusto
// Detection errors: a rule that raised. It's skipped for that event, so this is
// silent unless you look.
traces | where message contains "raised on a" | summarize count() by bin(timestamp, 1h)

// Log types arriving with no detection behind them - the coverage gap.
traces | where message contains "no detections are registered"

// Output failures. These mean records were DROPPED.
traces | where message contains "write failed" or message contains "POST failed"

// Storm limiter firing: a detection is producing more than STORM_LIMIT alerts/hour.
traces | where message contains "storm limit hit"

// Bundle reloads - confirms a publish went live, and when.
traces | where message contains "loaded detection bundle"
```

Set an alert rule on at least the third and fourth. The others are worth a weekly
look.

Health, from anywhere: `GET https://<app>.azurewebsites.net/api/health?code=<key>`
→ 200 when detections are loaded, 503 when they aren't. Point an uptime check at
it.

**Event Hub lag** is the metric that tells you whether you're keeping up: Event
Hubs namespace → Metrics → *Incoming Messages* vs *Outgoing Messages*. A widening
gap means scale up (more partitions, bigger plan, larger batch size), in that
order.

---

## Scaling

The engine is designed to be cheap at volume, and the levers are all config:

| Symptom | Lever |
|---|---|
| Falling behind on one source | More partitions on that hub. Parallelism is capped by partition count. |
| Falling behind everywhere | Bigger `maxEventBatchSize`, then a larger Premium plan / more max instances. |
| Cost too high at steady volume | Larger `maxEventBatchSize` — fewer invocations for the same events. |
| Redis is the bottleneck | Larger cache tier. State ops are already pipelined per batch, so this is rare. |
| One noisy detection | `Threshold:` and `DedupPeriodMinutes:` in its YAML, or `CreateAlert: false` to keep the signal and drop the alert. |

Detection count is close to free: routing is a dict lookup, so an event only ever
runs the detections registered for its own log type.

---

## Security posture

- **No secrets.** Every Azure dependency is reached by managed identity. The only
  secret-shaped settings are `OUTPUT_HTTP_URL` and `ALERT_WEBHOOK_URL` if those
  endpoints embed a token — put those in Key Vault and reference them
  (`@Microsoft.KeyVault(SecretUri=...)`).
- **Detections are code that runs in your Function App.** They come from a git
  repo and go through a pull request; treat that review as a security review.
- **The `health` and `ingest` endpoints are function-key authenticated.** Rotate
  the keys if one leaks (Function App → App keys). If you don't need `ingest` in
  prod, that's a fair thing to drop.
- **Network.** Public endpoints work but aren't the target state: private
  endpoints on storage and Redis, VNet integration on the Function App, and
  inbound restricted to what actually needs it. Note that locking down the
  Function App also locks down deploys — read
  [troubleshooting](troubleshooting.md#deploy-fails-with-403-or-failed-to-fetch)
  first.

---

## Spin-down

Delete the resource group. The detections repo, this repo and every published
bundle in git are the whole system; the Azure side is disposable and this guide
rebuilds it.
