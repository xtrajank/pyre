#!/usr/bin/env python3
"""Convenience: replay both starter sample files once. See python_shipper.py.

    EH_NAMESPACE=<prefix>-ehns.servicebus.windows.net EH_HUB=default-logs-in \
        python tools/testlab/replay_samples.py
"""
import subprocess, sys, os
NS = os.environ.get("EH_NAMESPACE", "<ns>.servicebus.windows.net")
# Must name a hub config/sources.yaml actually creates (palo-traffic-in,
# cloudflare-in, default-logs-in) AND the one the processor consumes. This was
# hardcoded to "logs-in", which sources.yaml has never created - see
# tools/PRODUCTION_CHECKLIST.md section 0.
HUB = os.environ.get("EH_HUB", "default-logs-in")
here = os.path.dirname(__file__)
for f in ("palo_sample.jsonl", "cloudflare_sample.jsonl"):
    subprocess.call([sys.executable, os.path.join(here, "python_shipper.py"),
                     "--namespace", NS, "--hub", HUB,
                     "--file", os.path.join(here, "samples", f), "--rate", "50"])
