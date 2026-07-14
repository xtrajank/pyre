# pyre

**pyre is the compute controller for a SIEM.** Logs stream in and pyre runs a library of **python-based detections** against those logs. When one matches it pushes and alert and a signal.

---

1. **[docs/GLOSSARY.md](docs/GLOSSARY.md)** — every term used anywhere in this repo, in plain English. 10 minutes. Come back here after.
2. **[docs/architecture.md](docs/architecture.md)** — the one-page "how it fits together and why," with the pipeline diagram.
3. **[docs/PRODUCTION.md](docs/PRODUCTION.md)** — the complete master guide: architecture, every variable, spinning up dev **and** prod end to end, monitoring, debugging, scaling, spin-down, and a script for walking leadership through it. This is the one you *operate* from.
4. **[docs/local-dev.md](docs/local-dev.md)** — the $0, no-Azure loop for developing/testing the engine and detections on your laptop.

Then dip into a directory's own `README.md` (table below) when you need depth on that piece.

---

## The mental model

```
   Logs from Okta, AWS,        pyre (this repo)                 A human
   firewalls, Cloudflare…      ┌───────────────────────┐       gets a case
        │                      │ 1. route by log type  │          ▲
        ▼                      │ 2. run the detections │          │
   ┌─────────┐  cleaned up   ┌─┴─ 3. is this new?      │   ┌──────┴──────┐
   │  Cribl  │──────────────▶│    (dedup, thresholds)  │──▶│    Torq     │
   └─────────┘   & routed    │ 4. open a case          │   │ (case tool) │
                             └───────────────────────┘   └─────────────┘
        detections come from ▲
        an external Git repo ─┘  (panther-analysis or your fork)
```

Full reasoning, and why each Azure piece was chosen to stay cheap at millions of logs/hour, is in [docs/architecture.md](docs/architecture.md).

---

## Repository map

Each directory has its own `README.md` that goes deep on that topic. Start at the top row and work down as needed.

| Directory | What lives here | Read its README when you want to… |
|---|---|---|
| [docs/](docs/) | All the guides (glossary, architecture, deploy runbook, security) | understand a concept or run a procedure |
| [config/](config/README.md) | Plain-text settings: which repo the detections come from, where logs arrive, where alerts go | onboard a log source, change a destination, point at a detections repo |
| [engine/](engine/README.md) | The actual detection processor (the Python that runs the rules) | understand or change how detections are executed |
| [cli/](cli/README.md) | The `pyre` command-line tool (`pull`, `build`, `publish`, `deploy`…) | pull detections, publish them, or deploy |
| [infra/](infra/README.md) | Terraform — the code that creates the Azure resources | create/change/destroy the cloud environment |
| [detections/](detections/README.md) | A signpost — real detections live in an **external** repo | learn where detections live and how they reach the engine |
| [tests/](tests/README.md) | Automated tests for the engine and detections | run the tests or add one |
| [tools/](tools/README.md) | Test-lab helpers: run it locally, ship sample logs, a fake alert sink | test on your laptop or feed sample data |

---

## Pointers

- **See it work on your laptop ($0, no Azure):** [docs/local-dev.md](docs/local-dev.md) — `python tools/testlab/run_local.py`.
- **Deploy an environment (dev or prod) to Azure:** [docs/PRODUCTION.md § 9](docs/PRODUCTION.md#9-spin-up-cloud) — provision, deploy the engine, publish detections, connect Cribl + Torq.
- **Add or change a detection:** edit it in your external detections repo and `git push` — pyre hot-reloads it within ~a minute. See [detections/README.md](detections/README.md).
- **Shut it all down:** `terraform destroy` — [docs/PRODUCTION.md § 17](docs/PRODUCTION.md#17-spinning-down).

---

## What's built

- **Built and tested:** the detection engine (routing → rule → signal → dedup/threshold → alert → dispatch), the external-DaC bundle loading + hot-reload, the local test runner, and the Terraform — **one composition you stamp out as dev/prod instances** (validates).
- **Structurally there, not yet exercised end-to-end on real Azure:** the CI publish-to-Blob pipeline, and a first cloud `apply` (the Terraform validates but hasn't been applied — expect to debug SKU/region/quota specifics on a free trial).
- **Deliberately out of scope for now:** log normalization (that's Cribl's job), scheduled-query detections, correlation rules, the AI triage agent, and a search UI. The architecture leaves clean seams for each — see [docs/architecture.md](docs/architecture.md) and [PANTHER_CONVERSION.md](PANTHER_CONVERSION.md).

## Design principles

- **Cheap at scale:** batch everything; one Azure Function execution evaluates hundreds of logs; scale to zero when idle.
- **No secrets in code:** every service is reached by identity (Managed Identity) or Key Vault reference, never a password in a file.
- **Modular:** add a log source or an alert destination by editing config, not code; replace any Terraform module without touching the others.
- **Detections stay portable:** they're plain code in their own repo.