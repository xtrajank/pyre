import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DAC = os.path.join(REPO, "dac")
__all__ = ["REPO", "DAC", "SAMPLES", "sample_messages"]
SAMPLES = os.path.join(REPO, "tools", "samples", "eventhub_diagnostic.jsonl")


def sample_messages():
    with open(SAMPLES, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]
