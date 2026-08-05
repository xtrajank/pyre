#!/usr/bin/env python3
"""Run the real detection engine on your laptop. No Azure, no cost, two seconds.

This is the fastest loop there is for "will my detection fire on my logs?" - the
same processor, registry, thresholds and dedup that run in the cloud, reading a
detections folder and a file of log lines.

    python tools/run_local.py
    python tools/run_local.py --bundle ../my-detections-repo --file my-logs.json
    python tools/run_local.py --log-type-field Category --event-time-field Timestamp

The three field flags mean exactly what the same-named keys in
config/sources.yaml mean. Getting them right here is what makes them right there.
"""
import argparse
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from pyre_engine.config import RuntimeConfig, Source          # noqa: E402
from pyre_engine.processor import Processor                   # noqa: E402


class CaptureSink:
    """Stands in for the blob/HTTP sink. Same records, kept in memory."""

    def __init__(self):
        self.records = []

    def write(self, records):
        self.records.extend(records)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=os.path.join(REPO, "dac"),
                    help="detections folder (default: the dac/ starter repo)")
    ap.add_argument("--file", default=os.path.join(HERE, "samples", "eventhub_diagnostic.jsonl"),
                    help="log messages, one JSON object per line")
    ap.add_argument("--log-type-field", default="Category")
    ap.add_argument("--event-time-field", default="Timestamp")
    ap.add_argument("--envelope-field", default="records", help="'' to disable")
    ap.add_argument("--json", action="store_true", help="dump the raw records instead")
    ap.add_argument("--log", default="WARNING",
                    help="engine log level. INFO shows the same per-batch line "
                         "Azure will show you (default: WARNING)")
    args = ap.parse_args()

    logging.basicConfig(level=args.log.upper(), format="%(levelname)-8s %(name)s  %(message)s")

    source = Source(hub="local", namespace="local", log_type_field=args.log_type_field,
                    event_time_field=args.event_time_field,
                    envelope_field=args.envelope_field)
    # Every environment-dependent setting is passed explicitly, so this run is
    # unaffected by whatever App settings happen to be exported in your shell.
    cfg = RuntimeConfig(sources=[source], detections_source="local",
                        detections_local_dir=args.bundle, detections_refresh_seconds=0,
                        state_backend="memory", redis_host="")
    sink = CaptureSink()
    proc = Processor(cfg, sink=sink)

    with open(args.file, encoding="utf-8") as fh:
        messages = [ln.strip() for ln in fh if ln.strip()]

    stats = proc.loader.get().stats()
    if not args.json:
        # --json emits a JSON document and nothing else, so it can be piped.
        print(f"bundle    {args.bundle}")
        print(f"          {stats['detections']} detection(s) covering "
              f"{stats['log_types'] or 'NOTHING - no detections loaded'}")
        print(f"routing   {args.log_type_field!r} on each record")
        print(f"input     {len(messages)} message(s) from {args.file}\n")

    proc.process_batch(messages, source)

    signals = [r for r in sink.records if r["p_record_type"] == "signal"]
    alerts = [r for r in sink.records if r["p_record_type"] == "alert"]

    if args.json:
        print(json.dumps(sink.records, indent=2, default=str))
        return

    print(f"SIGNALS  {len(signals)}   (one per rule() that returned True)")
    for s in signals:
        mark = "->alert" if s["p_alert_id"] else "  held "
        print(f"  {mark}  {s['p_detection_id']:34}  {s['p_dedup']}")
    print(f"\nALERTS   {len(alerts)}   (matches that also cleared Threshold and dedup)")
    for a in alerts:
        print(f"           [{a['p_severity']:6}] {a['p_title']}")
    if signals and not alerts:
        print("           none - every match was below its Threshold or grouped by dedup")
    if not signals:
        print("\nNo signals. Either nothing matched, or routing missed: compare the")
        print(f"log types above against the {args.log_type_field!r} value in your data.")


if __name__ == "__main__":
    main()
