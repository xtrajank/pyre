# tools/sim — test the whole pipeline end to end, dev or prod, for $0

Runs the **real** engine, the **real** CLI, and the **real** Terraform graph
against local stand-ins for everything outside pyre's boundary. No Azure, no
credentials, no cost, nothing leaves your machine unless you explicitly ask it
to.

> Commands are **PowerShell** (Windows). `docker compose` and `terraform` run on
> your host; anything after `run --rm sim` runs **inside the Linux container**, so
> those keep Linux syntax (e.g. `./cli/pyre`). Host CLI calls use `python cli/pyre`.

```powershell
# 1. the automated suite: 49 tests, real Redis 6.0
docker compose -f tools/sim/docker-compose.yml run --rm sim

# 2. drive logs through by hand and watch what comes out
docker compose -f tools/sim/docker-compose.yml run --rm sim `
    python tools/sim/run_pipeline.py

# 3. the Terraform plan, offline (mock provider)
terraform -chdir=infra test

# 4. done
docker compose -f tools/sim/docker-compose.yml down -v
```

---

## Why this exists alongside `tools/testlab/run_local.py`

`run_local.py` is the fast inner loop for **authoring a detection**. This is the
fidelity loop for **changing the engine or rehearsing a deploy**. It differs on
the axes where a local run silently lies:

| | `testlab/run_local.py` | `tools/sim` |
|---|---|---|
| Redis | fakeredis | **real Redis 6.0** — a conservative floor (Azure Managed Redis is 7.x) |
| Python | whatever you have | **3.11** — the Function App's `runtime_version` |
| DaC repo | a directory | **a real git repo**, cloned by the real `pyre pull` |
| Cribl / Torq | recorded | recorded, **and can return 5xx on demand** |
| Concurrency | single-threaded | **thread pool**, like the real worker |
| Routing | dev only | **dev *and* prod** branches |

The first two are not pedantry:

- **fakeredis accepts `EXPIRE key ttl NX`** (Redis 7.0+); a real Redis 6.0 rejects
  it. The engine's dedup Lua avoids that syntax so it runs on either, and
  `test_expire_nx_really_is_rejected_by_redis_60` pins the premise on the 6.0
  floor — fakeredis alone would hide a 6.0 incompatibility. (Azure Managed Redis
  is 7.x and would accept `EXPIRE NX`, so 6.0 here is the deliberately stricter
  target, not a mirror of what Azure serves.)
- **`pyre deps` is only as truthful as its interpreter matching the runtime's.**
  It resolves a bundle's imports against the environment it runs in, so a sim on
  3.12 would bless a bundle the 3.11 Function App cannot import.

---

## Scenario 1 — dev routing, sample logs (start here)

The safe default. `PYRE_ENV=dev`, so alerts route to `mock`; every URL points at
a local recording sink.

```powershell
docker compose -f tools/sim/docker-compose.yml run --rm sim `
    python tools/sim/run_pipeline.py --file tools/testlab/samples/palo_sample.jsonl
```

```text
redis   : 6.0.20 (real server)
events  : 4 from tools/testlab/samples/palo_sample.jsonl
env     : dev  ->  default route 'mock'

SIGNALS    (one per rule match): 2
ALERTS     (after threshold + dedup + storm limit): 1
DISPATCHED to 'mock': 1
```

Read that as: 4 events in, 2 matched (a signal each — the audit trail), both
shared a dedup string so they **collapsed into one alert**, which was delivered
once. That collapse is the dedup working.

## Scenario 2 — your own real logs

Capture live connections from this machine, then feed them through:

```powershell
pip install psutil
python tools/testlab/capture_netlogs.py -o tests/network_logs.jsonl   # repeat a few times

docker compose -f tools/sim/docker-compose.yml run --rm sim `
    python tools/sim/run_pipeline.py --file tests/network_logs.jsonl
```

`capture_netlogs.py` stands in for Cribl: it stamps `dataset` and `_time` on
each record, which is the one job a normalizer does before anything reaches
Event Hubs. Expect **0 signals** unless a connection happens to trip a rule in
your bundle — that is a correct result, not a failure. To prove the wiring,
append a line you know matches:

```powershell
'{"dataset":"ICO.Network","_time":"2026-07-15T00:00:00Z","remote_ip":"203.0.113.9","remote_port":4444,"process_name":"nc","hostname":"test-host"}' | Add-Content tests/network_logs.jsonl
```

## Scenario 3 — your own detections, pulled from your real DaC repo

```powershell
python cli/pyre pull        # clones config/detections.yaml's repo@ref into .bundle/
python cli/pyre validate    # LogTypes must exist in config/sources.yaml
python cli/pyre deps        # imports must resolve — but see the 3.11 note below

docker compose -f tools/sim/docker-compose.yml run --rm sim `
    python tools/sim/run_pipeline.py --bundle .bundle --file tests/network_logs.jsonl
```

Run `pyre deps` **inside the sim** to get the 3.11 answer that matches the
Function App, rather than your laptop's Python (this runs in the Linux container,
so it keeps `./cli/pyre`):

```powershell
docker compose -f tools/sim/docker-compose.yml run --rm sim ./cli/pyre deps
```

## Scenario 4 — rehearse PROD routing, without touching Torq

This is the one `run_local.py` cannot do. `--env prod` takes the real prod
branch (`default_routes = ["torq_prod"]`) through the real Torq adapter, but the
URL still points at the local sink:

```powershell
docker compose -f tools/sim/docker-compose.yml run --rm sim `
    python tools/sim/run_pipeline.py --file tools/testlab/samples/palo_sample.jsonl --env prod
```

```text
env     : prod  ->  default route 'torq_prod'
DISPATCHED to 'torq_prod': 1
```

**If this shows 0 dispatched, prod is broken and dev would never tell you.** It
is how the `torq_prod: enabled: false` defect was found: the Dispatcher skips
registering a disabled destination, so every prod alert failed to deliver, re-
opened its dedup window, re-fired, and failed again — forever, while dev looked
perfectly healthy.

## Scenario 5 — a real destination (deliberately hard to do by accident)

Everything above is hermetic. To send somewhere real you must opt in twice:

```powershell
# a real webhook, dev routing
docker compose -f tools/sim/docker-compose.yml run --rm sim `
    python tools/sim/run_pipeline.py --dest-url https://your-webhook --allow-external

# a real Torq endpoint on the PROD route — this opens real cases
docker compose -f tools/sim/docker-compose.yml run --rm `
    -e DESTINATION_TORQ_PROD_TOKEN=<token> sim `
    python tools/sim/run_pipeline.py --env prod --dest-url https://torq... `
        --allow-external --yes-really-page-prod
```

Without `--allow-external`, any non-localhost URL is refused. With `--env prod`
**and** an external URL, `--yes-really-page-prod` is required on top. See
[tools/PRODUCTION_CHECKLIST.md](../PRODUCTION_CHECKLIST.md) §1 — routing is
`default_routes` per instance, and a lab declares no production destination at all.

---

## The automated suite — what the 49 tests actually prove

```powershell
docker compose -f tools/sim/docker-compose.yml run --rm sim
docker compose -f tools/sim/docker-compose.yml run --rm sim `
    python -m pytest tools/sim/test_e2e.py -k dedup -v      # one area
```

- **State, against real 6.0** — dedup/threshold/unique Lua, a TTL on *every* key,
  bounded key size, first-event-wins alert marker, storm limiter.
- **DaC** — `pull` from real git, helper copying, sha stamping, no token on
  disk, `dac.include`/`dac.exclude` actually filtering; the `deps` gate
  (including imports hidden in a `rule()` body, and that the gate never
  *executes* DaC code).
- **Processing** — routing by log type, lazy per-log-type imports, one module
  exec ever, signals, dedup collapse, thresholds, alert context, dispatch.
- **The host's hub wiring** — one trigger registered per hub (including the
  catch-all), each bound to *its own* hub rather than all to the last one,
  event ids scoped per hub so two hubs can't collide in the idempotency set,
  and an empty hub list failing loudly instead of looking idle.
- **Prod routing** — prod alerts are actually deliverable; an unset
  `SIGNALS_SINK_URL` warns instead of silently discarding the audit trail.
- **No leakage** — redelivery, claim release on failure, a Cribl 5xx not read as
  success, dispatch failure re-opening the alert window, untrusted detections
  that raise, malformed JSON / missing log type counted rather than dropped.
- **Concurrency** — no lost signals and no duplicate alerts across 8 threads; no
  empty detection list on a racing first touch of a log type.

## Terraform, offline

```powershell
terraform -chdir=infra test
```

A `mock_provider` plans the entire graph with no subscription (16 checks, all
passing). It asserts the cost profile SKUs, that nothing is publicly reachable,
that no shared-key auth survives anywhere, that no `*_TOKEN` is a literal, and
that a **greenfield plan works at all** (a `count`/`for_each` on a
not-yet-created identity fails the plan outright — that regression is why the
check exists).

It also pins the hub wiring, which is the failure a plan read by eye cannot
catch because both sides are just strings: every hub the processor subscribes to
exists, every hub that exists has a consumer, there is exactly one `default:
true` catch-all and it is consumed, and the catch-all stays the cheapest hub.

---

## Troubleshooting

**"0 signals" and `batch dropped N/N event(s): ... missing the 'log type' field"`**
Your events' log-type field name doesn't match `LOG_TYPE_FIELD`. The engine
defaults to Cribl's `dataset`/`_time`, **not** Panther's `p_log_type`/
`p_event_time`. Either restamp the events or set `log_type_field` /
`event_time_field` (Terraform) to what your feed actually uses.

**"0 signals", no drop warning**
The events routed fine but nothing matched. Check that your detections'
`LogTypes:` values equal the `dataset` values on your events, exactly.

**`skipping detection <id>`**
That detection failed to import — a missing helper, or a package the engine
doesn't have. This is the silent coverage hole `pyre deps` exists to catch.

**Redis version assertion fails**
Someone bumped the image off 6.x. Don't — the 6.0 floor is deliberately stricter
than Azure Managed Redis (7.x), so it catches 7.0-only syntax that fakeredis and
a 7.x server would both wave through.

## What it does NOT cover

- **The Event Hubs trigger.** The sim calls `Processor.process_batch` directly,
  which is what `function_app.py` does with the decoded batch. Trigger binding,
  checkpointing and partition ownership are Azure's.
- **TLS/Entra to Redis.** `StateStore` hardcodes `ssl=True` on the connection it
  builds itself, so the sim injects a plain client — the same seam `tests/` and
  `run_local.py` use. The first real Redis call in Azure is where an auth
  problem would surface.
- **`BlobBundleSource`.** The sim exercises `LocalBundleSource`; the blob path
  needs real storage.
- **Cribl itself**, including whether it can authenticate to Event Hubs with
  Entra (`local_authentication_enabled = false` disables SAS entirely — the most
  likely day-one surprise; checklist §3).

## The two pytest suites are separate — run them separately

`pytest tests` and `pytest tools/sim` must not be collected in one invocation.
Both directories are non-packages containing a `conftest.py`, so pytest imports
whichever it reaches first as the top-level module `conftest`, and
`tests/test_registry_loader.py`'s `from conftest import SAMPLE_DAC` then
resolves against the wrong one. CI runs `tests/`; compose runs `tools/sim/`.
