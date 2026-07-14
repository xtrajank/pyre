# Local development & testing ($0, no Azure)

This is the loop for developing and testing the **engine and detection logic that live in this repo**, entirely on your laptop, for free. It's the one environment meant to stay permanent for feature work. For deploying to Azure (dev/prod), see **[PRODUCTION.md](PRODUCTION.md)**.

It runs the **real** engine code (`engine/pyre_engine/*`). Only three things are swapped for local stand-ins, each behind a clean seam:
- Redis → in-memory `fakeredis`,
- the Cribl lake sink and the Torq/mock destination → a tiny local HTTP server that just records what it receives.

Everything else is identical to what runs in Azure.

## 1. One-time setup

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# Git Bash / macOS:     source .venv/bin/activate
pip install -r engine/requirements.txt fakeredis pytest
```

## 2. Run the tests

```bash
python -m pytest tests -q
```

Read `tests/test_registry_loader.py` — it proves detections load from a bundle directory (the external-DaC stand-in), route by `LogType`, and **hot-reload** when the bundle version or the enabled-set changes. That's the "detections live in another repo and a push reflects fast" behavior, tested without a network. See [tests/README.md](../tests/README.md).

## 3. Run the whole engine against sample logs

```bash
python tools/testlab/run_local.py
```

Expected output (annotated):

```
SIGNALS  written to lake (one per rule match): 2      <- 2 RDP logs matched
  match  palo_traffic_high_risk_port  dedup=10.1.1.5:3389
  match  palo_traffic_high_risk_port  dedup=10.1.1.5:3389
ALERTS   (after threshold + dedup + storm-limit): 1   <- both collapsed into ONE
  fire   [HIGH    ] Palo: allowed traffic to high-risk port 3389 from 10.1.1.5
DISPATCHED to destination (mock): 1                   <- the Torq-case call, in prod
```

Read it against `tools/testlab/samples/palo_sample.jsonl` (4 logs): two RDP-to-3389 `allow`s match, a `443 allow` and a `telnet deny` don't. Two matches → two **signals** (the audit trail), but they share a dedup string so they collapse into **one alert** (first-event-wins). Then try the threshold case:

```bash
python tools/testlab/run_local.py --file tools/testlab/samples/cloudflare_sample.jsonl
```

One SQLi request matches → **1 signal, 0 alerts**, because that detection sets `Threshold: 5`. A single match is recorded but doesn't page anyone — which is exactly why signals and alerts are separate.

Add `--html report.html` to either command to also get a browser-viewable report (stat tiles + a table each for signals/alerts/dispatched) instead of reading console output.

## 4. Change a detection and watch it take effect

- Open `tests/fixtures/sample_dac/cloudflare/cloudflare_http_sqli.yml`, change `Threshold: 5` to `1`, re-run the Cloudflare command → now it alerts. In production, that same edit is a `git push` to your detections repo.
- Duplicate an RDP line in the Palo sample with a **different** `src_ip` → two alerts (different dedup strings). Same `src_ip` → still one alert, more signals.

## 5. (Optional) run against your real detections repo

```bash
python cli/pyre pull                         # clone config/detections.yaml dac.repo -> ./.bundle
python cli/pyre validate
python tools/testlab/run_local.py --bundle .bundle --file <your-logs>.jsonl
```

Caveat: the registry parses **classic** paired `.py`+`.yml` detections (`RuleID`/`Filename`/`LogTypes`), not the class-based `pypanther` style. Point `dac.repo`/`dac.path` at a folder of classic detections (or your fork) before `pull`.

---

**That's the whole local loop.** When you're ready to run it in Azure, go to **[PRODUCTION.md](PRODUCTION.md)** — start at §8 (which points back here), then §9 onward to provision dev or prod.
