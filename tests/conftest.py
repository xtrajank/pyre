import os, sys

# The engine package (so tests can import pyre_engine.*).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

# The offline fake-DaC bundle used by tests (stands in for the external repo).
SAMPLE_DAC = os.path.join(os.path.dirname(__file__), "fixtures", "sample_dac")
