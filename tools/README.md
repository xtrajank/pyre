# tools/ — test-lab helpers (never deployed to production)

Everything here exists to help you **test and understand** pyre. None of it runs in a real deployment; the only test-only pieces of the whole system are the fake log *source* (here) and the fake alert *destination* (here). Both are swapped for the real thing by editing config, not code.

New to the terms? See the [glossary](../docs/GLOSSARY.md).

## What's here

| Path | What it is | Use it to… |
|---|---|---|
| `testlab/run_local.py` | Runs the **real** detection engine on your laptop with a fake in-memory Redis and a local sink. Prints the signals and alerts it produces. | Understand and test the whole pipeline for **$0**, no Azure. Start here. |
| `testlab/samples/*.jsonl` | Starter sample logs (Palo firewall, Cloudflare HTTP), one JSON log per line. | Feed the engine realistic-shaped events. |
| `testlab/python_shipper.py` | Replays a `.jsonl` file into a *cloud* Event Hub at a chosen rate. | Drive a deployed lab / load-test scaling. |
| `testlab/fluent-bit.conf` | Config to ship **live** logs from a PC or Raspberry Pi into Event Hubs. | Feed continuous real data (advanced). |
| `testlab/replay_samples.py` | Convenience wrapper over the starter samples. | Quick replay. |
| `mocks/mock_destination/` | A tiny Azure Function that just logs whatever alert it receives. | Give alerts somewhere to go in a lab, so you can watch the loop end-to-end. |

## The $0 local run (do this first)

```bash
pip install -r engine/requirements.txt fakeredis   # one-time
python tools/testlab/run_local.py                   # Palo sample: matches + dedup
python tools/testlab/run_local.py --file tools/testlab/samples/cloudflare_sample.jsonl
python tools/testlab/run_local.py --bundle .bundle  # against a real `pyre pull` bundle
```

It reads a detection **bundle** (default: the offline test fixture) and a `.jsonl` of logs, runs `engine/pyre_engine/processor.py` over them, and prints:
- **SIGNALS** — one per detection match (the audit trail),
- **ALERTS** — what survived threshold + dedup,
- **DISPATCHED** — what was sent to the (mock) destination.

Full walkthrough with what each number means: [docs/local-dev.md](../docs/local-dev.md).

## Driving a deployed lab

A deployed environment is private (logs come from Cribl). To push samples yourself for a one-off demo, you temporarily allow your IP on the Event Hub, then remove it:

```bash
python tools/testlab/python_shipper.py \
  --namespace <name_prefix>-ehns.servicebus.windows.net --hub logs-in \
  --file tools/testlab/samples/palo_sample.jsonl --rate 50
```

The exact `az` commands to open and re-close the firewall, and where to watch the results in Azure Log Analytics, are in [docs/PRODUCTION.md § 14](../docs/PRODUCTION.md#14-debugging).

## The mock destination

`mocks/mock_destination/` is deployed only in a lab (`func azure functionapp publish <name_prefix>-mockdest`). Its URL is fed to the engine as `MOCK_DEST_URL` so fired alerts land somewhere you can see them. Swap it for real Torq by editing `config/destinations.yaml` — no engine change.
