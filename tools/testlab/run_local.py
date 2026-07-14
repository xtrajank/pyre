#!/usr/bin/env python3
"""Run the REAL detection processor on your laptop, with ZERO Azure, for $0.

Proves the whole loop that matters: external-DaC bundle -> per-log-type routing
-> rule() -> signal on every match -> dedup/threshold -> alert -> dispatch.
Everything is the same code that runs in Azure, EXCEPT:
  * Redis            -> in-memory fakeredis (no TLS, no Entra, no cost)
  * Cribl signals    -> a tiny local HTTP server that just records what arrives
  * mock destination -> the same local HTTP server

    pip install -r engine/requirements.txt fakeredis
    python tools/testlab/run_local.py                 # Palo sample (matches + dedup)
    python tools/testlab/run_local.py --file tools/testlab/samples/cloudflare_sample.jsonl
    python tools/testlab/run_local.py --bundle .bundle # a real `pyre pull` bundle
    python tools/testlab/run_local.py --html report.html   # also write a visual report
"""
import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "engine"))

# SignalWriter posts signals AND alert records to the SAME Cribl sink (the lake
# write-back from architecture Part 9), tagged by `_dataset`; the dispatcher posts
# the delivered alert separately to the destination. We split them back out here.
CAPTURED = {"signals": [], "alert_records": [], "dispatched": []}


class _Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "null")
        if self.path.endswith("/signals"):        # SignalWriter batch (signals + alert records)
            for rec in (body if isinstance(body, list) else [body]):
                bucket = "alert_records" if rec.get("_dataset") == "pyre_alerts" else "signals"
                CAPTURED[bucket].append(rec)
        else:                                      # dispatcher -> the destination (mock)
            CAPTURED["dispatched"].append(body)
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _start_sink() -> int:
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=os.path.join(REPO, "tests", "fixtures", "sample_dac"),
                    help="local detection bundle dir (default: the offline sample DaC)")
    ap.add_argument("--file", default=os.path.join(HERE, "samples", "palo_sample.jsonl"),
                    help="newline-delimited JSON events to feed the processor")
    ap.add_argument("--html", default=None,
                    help="also write a self-contained HTML report (stat tiles + "
                         "signal/alert/dispatch tables) to this path")
    args = ap.parse_args()

    port = _start_sink()
    # Set env BEFORE importing/loading config so the engine reads local settings.
    os.environ.update(
        PYRE_ENV="dev",
        BUNDLE_MODE="local",
        BUNDLE_LOCAL_DIR=args.bundle,
        SIGNALS_SINK_URL=f"http://127.0.0.1:{port}/signals",
        MOCK_DEST_URL=f"http://127.0.0.1:{port}/alert",
        DESTINATIONS_PATH=os.path.join(REPO, "config", "destinations.yaml"),
    )

    import fakeredis
    from pyre_engine.config import load_runtime_config
    from pyre_engine.dedup import StateStore
    from pyre_engine.processor import Processor

    cfg = load_runtime_config()
    state = StateStore("", 0, use_entra=False, client=fakeredis.FakeStrictRedis(decode_responses=True))
    proc = Processor(cfg, state=state)

    events = [ln.strip() for ln in open(args.file, encoding="utf-8") if ln.strip()]
    print(f"bundle : {os.path.relpath(args.bundle, REPO)}")
    print(f"events : {len(events)} from {os.path.relpath(args.file, REPO)}\n")

    proc.process_batch(events)

    print(f"SIGNALS  written to lake (one per rule match): {len(CAPTURED['signals'])}")
    for s in CAPTURED["signals"]:
        print(f"  match  {s['detection_id']:32}  dedup={s['dedup']}")
    print(f"\nALERTS   (after threshold + dedup + storm-limit): {len(CAPTURED['alert_records'])}")
    for a in CAPTURED["alert_records"]:
        print(f"  fire   [{a['severity']:8}] {a['title']}")
    if not CAPTURED["alert_records"]:
        print("  (none - matches were below their detection's Threshold, or grouped by dedup)")
    print(f"\nDISPATCHED to destination (mock): {len(CAPTURED['dispatched'])}  "
          f"(== alerts; this is the Torq-case call in prod)")

    print("\nWhat just happened:")
    print("  * every rule()==True wrote a SIGNAL (audit of everything that matched)")
    print("  * matches sharing a dedup string collapsed into ONE alert (first-event-wins)")
    print("  * a match below its YAML Threshold produced a signal but NO alert")
    print("  * non-matching events produced nothing - same as Panther")

    if args.html:
        from html_report import render
        meta = {
            "bundle": os.path.relpath(args.bundle, REPO),
            "file": os.path.relpath(args.file, REPO),
            "event_count": len(events),
        }
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render(meta, CAPTURED))
        print(f"\nHTML report -> {args.html}")


if __name__ == "__main__":
    main()
