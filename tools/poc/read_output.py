#!/usr/bin/env python3
"""Read back what the engine appended to Blob - the POC's "did it work?" check.

The engine writes two streams into one container (engine/pyre_engine/blobsink.py):

    alerts/<UTC date>.jsonl    what was DELIVERED to a destination  <- the demo
    signals/<UTC date>.jsonl   every rule() match, plus each alert record

    az login
    python tools/poc/read_output.py --account-url https://<storage>.blob.core.windows.net
    python tools/poc/read_output.py --account-url ... --stream signals --raw
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-url", default=os.environ.get("OUTPUT_BLOB_ACCOUNT_URL"),
                    help="e.g. https://<storage>.blob.core.windows.net")
    ap.add_argument("--container", default="pyre-output")
    ap.add_argument("--stream", default="alerts", choices=["alerts", "signals"])
    ap.add_argument("--date", default=None, help="UTC date YYYY-MM-DD (default: today)")
    ap.add_argument("--raw", action="store_true", help="print the raw JSON lines")
    args = ap.parse_args()

    if not args.account_url:
        sys.exit("read: --account-url (or OUTPUT_BLOB_ACCOUNT_URL) is required")

    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = f"{args.stream}/{day}.jsonl"

    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    blob = BlobServiceClient(args.account_url, credential=DefaultAzureCredential()) \
        .get_blob_client(args.container, name)
    try:
        body = blob.download_blob().readall().decode("utf-8")
    except Exception as exc:
        sys.exit(f"read: could not read {args.container}/{name}: {type(exc).__name__}: {exc}\n"
                 f"      (nothing written yet for {day}, or the identity lacks "
                 f"Storage Blob Data Reader)")

    records = [json.loads(l) for l in body.splitlines() if l.strip()]
    print(f"{args.container}/{name}: {len(records)} record(s)\n")
    if args.raw:
        print(body)
        return

    if args.stream == "alerts":
        for r in records:
            print(f"[{r.get('severity','?'):8}] {r.get('title','')}")
            print(f"           detection={r.get('detection_id')}  dedup={r.get('dedup')}")
            print(f"           context={json.dumps(r.get('context', {}))}\n")
    else:
        alerts = [r for r in records if r.get("_dataset") == "pyre_alerts"]
        signals = [r for r in records if r.get("_dataset") != "pyre_alerts"]
        print(f"signals (one per rule() match): {len(signals)}")
        for r in signals:
            print(f"  match  {r.get('detection_id',''):34} dedup={r.get('dedup')}")
        print(f"\nalert records (threshold + dedup passed): {len(alerts)}")
        for r in alerts:
            print(f"  fire   [{r.get('severity','?'):8}] {r.get('title','')}")


if __name__ == "__main__":
    main()
