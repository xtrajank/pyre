# tools/ — test-lab helpers (never deployed to production)

Everything here exists to help you **test and understand** pyre. None of it runs in a real deployment; the only test-only pieces of the whole system are the fake log *source* (here) and the fake alert *destination* (here). Both are swapped for the real thing by editing config, not code.

New to the terms? See the [glossary](../docs/GLOSSARY.md).

## What's here

| Path | What it is | Use it to… |
|---|---|---|
| **`PRODUCTION_CHECKLIST.md`** | **The pre-deploy checklist.** Blockers, the dev-vs-prod routing rules, Redis sizing, leak checks, the silent-drop alarm query. | **Read this before you deploy anything.** |
| `testlab/run_local.py` | Runs the **real** detection engine on your laptop with a fake in-memory Redis and a local sink. Prints the signals and alerts it produces. | Understand and test the whole pipeline for **$0**, no Azure. Start here. |
| **`sim/`** | The same engine against a **real Redis 6.0**, on **Python 3.11**, with a real git DaC repo and both the dev **and prod** routing branches. Docker. | Rehearse a deploy, or change the engine with confidence. `run_local.py` cannot show you the prod route or real Redis. |
| `testlab/samples/*.jsonl` | Starter sample logs (Palo firewall, Cloudflare HTTP), one JSON log per line. Stamped with `dataset`/`_time` — the engine's default field names. | Feed the engine realistic-shaped events. |
| `testlab/capture_netlogs.py` | Snapshots this machine's live network connections as pyre-shaped JSON (stamps `dataset`/`_time`, i.e. does Cribl's job). Needs `pip install psutil`. | Generate **real** logs from your own machine. |
| `testlab/python_shipper.py` | Replays a `.jsonl` file into a *cloud* Event Hub at a chosen rate. | Drive a deployed lab / load-test scaling. |
| `testlab/fluent-bit.conf` | Config to ship **live** logs from a PC or Raspberry Pi into Event Hubs. | Feed continuous real data (advanced). |
| `testlab/replay_samples.py` | Convenience wrapper over the starter samples. | Quick replay. |
| `mocks/mock_destination/` | A tiny Azure Function that just logs whatever alert it receives. | Give alerts somewhere to go in a lab, so you can watch the loop end-to-end. |

## Alerts never reach Torq from a lab — as long as this holds

Routing is explicit per instance — `default_routes` in that instance's tfvars,
naming destinations from `config/destinations.yaml`:

```hcl
destinations   = { mock = { url = "https://<mock-func>/api/alert" } }
default_routes = ["mock"]
```

The engine no longer infers routing from `env`. It used to be
`["mock"] if env == "dev" else ["torq_prod"]`, so **any** env string that wasn't
exactly `"dev"` — `"lab"`, `"Dev"`, a typo — routed to production.

What keeps a lab safe is that a dev instance simply **declares no production
destination**: with no URL and no token the adapter raises before it can POST
anywhere, and it isn't in `default_routes` either. Full rules:
[PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md) §1.

## The $0 local run (do this first)

```bash
pip install -r engine/requirements.txt fakeredis   # one-time
python tools/testlab/run_local.py                   # Palo sample: matches + dedup
python tools/testlab/run_local.py --file tools/testlab/samples/cloudflare_sample.jsonl
python tools/testlab/run_local.py --bundle .bundle  # against a real `pyre pull` bundle
python tools/testlab/run_local.py --html report.html  # also write a visual report
```

It reads a detection **bundle** (default: the offline test fixture) and a `.jsonl` of logs, runs `engine/pyre_engine/processor.py` over them, and prints:
- **SIGNALS** — one per detection match (the audit trail),
- **ALERTS** — what survived threshold + dedup,
- **DISPATCHED** — what was sent to the (mock) destination.

On the Palo sample that's **4 events → 2 signals → 1 alert → 1 dispatch**: two
events matched, both shared a dedup string, so they collapsed into one alert.

> **Seeing `SIGNALS: 0` and `batch dropped 4/4 ... missing the 'log type' field`?**
> Your events' log-type field doesn't match `LOG_TYPE_FIELD`. The engine defaults
> to Cribl's `dataset`/`_time`, **not** Panther's `p_log_type`/`p_event_time`.
> Restamp the events, or set the `log_type_field`/`event_time_field` Terraform
> variables to whatever your feed actually stamps.

Add `--html <path>` to also write a self-contained HTML report (stat tiles for
event/signal/alert/dispatch counts, plus a table for each) — open it in a
browser instead of reading console output. See `testlab/html_report.py`.

Full walkthrough with what each number means: [docs/local-dev.md](../docs/local-dev.md).

## The higher-fidelity run (before you deploy)

`run_local.py` uses fakeredis, which accepts Redis 7.0 syntax that Azure Cache
for Redis 6.0 rejects — so it cannot tell you whether dedup will work in Azure.
It also only ever exercises the **dev** route. When you're rehearsing a deploy
or changing the engine, use the Docker sim instead:

```bash
docker compose -f tools/sim/docker-compose.yml run --rm sim              # 45 tests, real Redis 6.0
docker compose -f tools/sim/docker-compose.yml run --rm sim \
    python tools/sim/run_pipeline.py --env prod                          # rehearse PROD routing safely
docker compose -f tools/sim/docker-compose.yml down -v
```

See [sim/README.md](sim/README.md) for the full dev/prod, real-logs/real-
destinations walkthrough.

## Driving a deployed lab

A deployed environment is private (logs come from Cribl). To push samples yourself for a one-off demo, you temporarily allow your IP on the Event Hub, then remove it:

```bash
python tools/testlab/python_shipper.py \
  --namespace <name_prefix>-ehns.servicebus.windows.net --hub <your-hub> \
  --file tools/testlab/samples/palo_sample.jsonl --rate 50
```

> `--hub` must name a hub `config/sources.yaml` creates — `terraform output
> eventhub_hub_names`. The processor consumes all of them. If your log type has
> no dedicated hub, send it to the catch-all (`terraform output
> default_eventhub_name`); it is evaluated identically there.

The exact `az` commands to open and re-close the firewall, and where to watch the results in Azure Log Analytics, are in [docs/PRODUCTION.md § 14](../docs/PRODUCTION.md#14-debugging).

## The mock destination

`mocks/mock_destination/` is deployed only in a lab (`func azure functionapp publish <name_prefix>-mockdest`). Point the instance at it with `destinations = { mock = { url = "https://<name_prefix>-mockdest.azurewebsites.net/api/alert" } }` and `default_routes = ["mock"]`; Terraform publishes that as `DESTINATION_MOCK_URL`, which is the `url_env` `config/destinations.yaml` names. Swapping it for a real destination is a tfvars entry plus a `kind` in that file — no engine change, and nothing in `infra/` is named after a vendor.
