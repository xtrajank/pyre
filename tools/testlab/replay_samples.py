#!/usr/bin/env python3
"""Convenience: replay both starter sample files once. See python_shipper.py."""
import subprocess, sys, os
NS = os.environ.get("EH_NAMESPACE", "<ns>.servicebus.windows.net")
here = os.path.dirname(__file__)
for f in ("palo_sample.jsonl", "cloudflare_sample.jsonl"):
    subprocess.call([sys.executable, os.path.join(here, "python_shipper.py"),
                     "--namespace", NS, "--hub", "logs-in",
                     "--file", os.path.join(here, "samples", f), "--rate", "50"])
