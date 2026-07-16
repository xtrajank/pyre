#!/usr/bin/env python3
"""Drive one batch of logs through the REAL engine inside the sim, and print
what came out.

This is tools/testlab/run_local.py's higher-fidelity sibling. Same idea - feed a
.jsonl, watch signals/alerts/dispatch - but against a real Redis 6.0 instead of
fakeredis, and with the DEV vs PROD routing branch selectable, which is the one
thing run_local.py cannot show you.

    # dev routing (-> "mock"), local sink, sample logs. The safe default.
    docker compose -f tools/sim/docker-compose.yml run --rm sim \
        python tools/sim/run_pipeline.py

    # your own captured logs
    docker compose -f tools/sim/docker-compose.yml run --rm sim \
        python tools/sim/run_pipeline.py --file tests/network_logs.jsonl

    # PROD routing (-> "torq_prod"), still landing in the local sink.
    # Proves the prod branch works without touching real Torq.
    docker compose -f tools/sim/docker-compose.yml run --rm sim \
        python tools/sim/run_pipeline.py --env prod

SAFETY. By default every destination URL points at a local recording sink, so
nothing can leave the container. Sending anywhere else requires an explicit
--allow-external, and prod-routed traffic to a real URL additionally requires
--yes-really-page-prod. See "Dev vs prod" in tools/PRODUCTION_CHECKLIST.md: the
engine routes to torq_prod for ANY env that is not exactly the string "dev".
"""
import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

REPO = "/pyre"
sys.path.insert(0, os.path.join(REPO, "engine"))

CAPTURED = {"signals": [], "alert_records": [], "dispatched": []}


class _Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "null")
        if self.path.endswith("/signals"):
            for rec in (body if isinstance(body, list) else [body]):
                bucket = "alert_records" if rec.get("_dataset") == "pyre_alerts" else "signals"
                CAPTURED[bucket].append(rec)
        else:
            CAPTURED["dispatched"].append(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _start_sink() -> int:
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default="tools/testlab/samples/cloudflare_sample.jsonl",
                    help="newline-delimited JSON events (default: the Cloudflare sample)")
    ap.add_argument("--bundle", default="tests/fixtures/sample_dac",
                    help="detection bundle dir (default: the offline sample DaC). "
                         "Use a real `pyre pull` output to test YOUR detections.")
    ap.add_argument("--env", default="dev", choices=["dev", "prod"],
                    help="dev routes alerts to 'mock'; prod routes to 'torq_prod'")
    ap.add_argument("--dest-url", default=None,
                    help="override the alert destination (default: the local sink)")
    ap.add_argument("--signals-url", default=None,
                    help="override the Cribl signals sink (default: the local sink). "
                         "Pass the empty string to reproduce the 'signals discarded' warning.")
    ap.add_argument("--allow-external", action="store_true",
                    help="permit a non-localhost destination URL")
    ap.add_argument("--yes-really-page-prod", action="store_true",
                    help="required to send PROD-routed alerts to a real external URL")
    args = ap.parse_args()

    port = _start_sink()
    local_signals = f"http://127.0.0.1:{port}/signals"
    local_dest = f"http://127.0.0.1:{port}/alert"

    dest = args.dest_url if args.dest_url is not None else local_dest
    signals = args.signals_url if args.signals_url is not None else local_signals

    # --- guardrails -------------------------------------------------------
    for label, url in (("destination", dest), ("signals sink", signals)):
        if url and not _is_local(url) and not args.allow_external:
            print(f"REFUSING: {label} {url!r} is not local. Re-run with "
                  f"--allow-external if you really mean to send outside the sim.")
            return 2
    if args.env == "prod" and dest and not _is_local(dest) and not args.yes_really_page_prod:
        print(f"REFUSING: --env prod routes to 'torq_prod' and {dest!r} is a real "
              f"endpoint. That would open real cases. Add --yes-really-page-prod "
              f"if that is genuinely what you want.")
        return 2

    os.environ.update(
        PYRE_ENV=args.env,
        BUNDLE_MODE="local",
        BUNDLE_LOCAL_DIR=os.path.join(REPO, args.bundle) if not os.path.isabs(args.bundle) else args.bundle,
        SIGNALS_SINK_URL=signals,
        DESTINATIONS_PATH=os.path.join(REPO, "config", "destinations.yaml"),
        # Destination values, under the generic DESTINATION_<NAME>_* names
        # Terraform generates from var.destinations. Nothing here is named after
        # a vendor; `kind` lives in config/destinations.yaml.
        DESTINATION_MOCK_URL=dest,
        DESTINATION_TORQ_PROD_URL=dest,
        # The torq adapter raises unless a token is present. A placeholder keeps
        # the sim's prod run exercising the real adapter; a real deployment
        # resolves this from Key Vault via destinations.<name>.token_secret.
        DESTINATION_TORQ_PROD_TOKEN=os.environ.get("DESTINATION_TORQ_PROD_TOKEN", "sim-placeholder-token"),
        # Routing is explicit per instance now, never inferred from PYRE_ENV.
        DEFAULT_ROUTES="mock" if args.env == "dev" else "torq_prod",
    )

    import redis
    from pyre_engine.config import load_runtime_config
    from pyre_engine.dedup import StateStore
    from pyre_engine.processor import Processor

    client = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                         port=int(os.environ.get("REDIS_PORT", "6379")),
                         decode_responses=True)
    version = client.info("server")["redis_version"]
    client.flushall()      # a clean dedup window, so re-runs are reproducible

    state = StateStore("", 0, use_entra=False, client=client)
    proc = Processor(load_runtime_config(), state=state)

    path = args.file if os.path.isabs(args.file) else os.path.join(REPO, args.file)
    events = [ln.strip() for ln in open(path, encoding="utf-8") if ln.strip()]

    route = "mock" if args.env == "dev" else "torq_prod"
    print(f"redis   : {version} (real server)")
    print(f"bundle  : {args.bundle}")
    print(f"events  : {len(events)} from {args.file}")
    print(f"env     : {args.env}  ->  default route '{route}'")
    print(f"dest    : {dest}{'' if _is_local(dest) else '   *** EXTERNAL ***'}\n")

    # Event ids stand in for Event Hubs' partition:sequence. Reusing this file's
    # own line numbers makes a second run of the SAME file look like a
    # redelivery, which is worth seeing on purpose.
    proc.process_batch(events, event_ids=[f"sim:{i}" for i in range(len(events))])

    print(f"SIGNALS    (one per rule match): {len(CAPTURED['signals'])}")
    for s in CAPTURED["signals"]:
        print(f"   match  {s['detection_id']:34} dedup={s['dedup']}")
    print(f"\nALERTS     (after threshold + dedup + storm limit): {len(CAPTURED['alert_records'])}")
    for a in CAPTURED["alert_records"]:
        print(f"   fire   [{a['severity']:8}] {a['title']}")
    if not CAPTURED["alert_records"]:
        print("   (none - matches were below Threshold, or grouped by dedup)")
    print(f"\nDISPATCHED to '{route}': {len(CAPTURED['dispatched'])}")
    for d in CAPTURED["dispatched"]:
        print(f"   sent   {d['detection_id']:34} context={json.dumps(d.get('context', {}))}")

    if not CAPTURED["signals"]:
        print("\nNo matches. That is expected if none of your events trip a rule in "
              "this bundle - it is not a failure. Check that your events' log-type "
              "field matches the detections' LogTypes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
