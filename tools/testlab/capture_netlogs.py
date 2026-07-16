#!/usr/bin/env python3
"""Capture this machine's live network connections as pyre-shaped JSON logs.

Stands in for Cribl: it emits one JSON object per connection with the routing
field (`dataset`) and the time field (`_time`) already stamped, which is the one
job a normalizer does before anything reaches Event Hubs. Feed the result to
tools/testlab/run_local.py or tools/testlab/python_shipper.py. See LAB.md.

    pip install psutil
    python tools/testlab/capture_netlogs.py [-o network_logs.jsonl]

Reading other processes' connections needs privileges: run as Administrator on
Windows, or with sudo on macOS/Linux. Without them `process_name` degrades to
"unknown" rather than failing.

NOT a pytest module. It lived in tests/ as test_network_logs.py, where pytest
collected it: it defines no tests, and its psutil import - which is not in
tests/requirements.txt - failed collection for the ENTIRE suite, so `pyre test`
and CI could not run at all.
"""
import argparse
import json
import os
import socket
from datetime import datetime, timezone

import psutil


def snapshot_connections():
    """Return one JSON-shaped record per active network connection."""
    records = []
    now = datetime.now(timezone.utc).timestamp()

    for conn in psutil.net_connections(kind="inet"):
        if not conn.raddr:
            continue  # skip connections with no remote address (listeners, etc.)

        try:
            proc_name = psutil.Process(conn.pid).name() if conn.pid else "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            proc_name = "unknown"

        record = {
            # This is the field pyre routes on. Cribl would set this in
            # production; we set it ourselves here to simulate that step.
            "dataset": "ICO.Network",
            "_time": now,
            "local_ip": conn.laddr.ip if conn.laddr else None,
            "local_port": conn.laddr.port if conn.laddr else None,
            "remote_ip": conn.raddr.ip,
            "remote_port": conn.raddr.port,
            "protocol": "tcp" if conn.type == socket.SOCK_STREAM else "udp",
            "status": conn.status,
            "process_name": proc_name,
            "pid": conn.pid,
            "hostname": socket.gethostname(),
        }
        records.append(record)

    return records


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    # Was a hardcoded "tests\\network_logs.jsonl" - a Windows-only literal that
    # wrote a backslash-named file on macOS/Linux, into whatever directory it
    # happened to be run from.
    ap.add_argument("-o", "--out", default="network_logs.jsonl",
                    help="append the snapshot to this .jsonl (default: ./network_logs.jsonl)")
    args = ap.parse_args()

    records = snapshot_connections()
    # If a previous run died mid-write (or two runs raced), the file can end on a
    # partial line. Appending straight onto it splices our first record into that
    # fragment and produces one unparseable line - which the engine then counts
    # and drops. Cheap to prevent, invisible to debug.
    if os.path.exists(args.out) and os.path.getsize(args.out) > 0:
        with open(args.out, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) not in (b"\n", b"\r"):
                with open(args.out, "a", encoding="utf-8") as fh2:
                    fh2.write("\n")

    with open(args.out, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    print(f"wrote {len(records)} connection(s) to {args.out} "
          f"at {datetime.now(timezone.utc).isoformat()}")