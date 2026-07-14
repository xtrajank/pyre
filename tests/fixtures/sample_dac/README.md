# tests/fixtures/sample_dac/

A tiny **fake** DaC repo used only by the test suite. It lets the engine and the
BundleLoader be tested offline - no clone of the real DaC repo, no network, no
Azure. It is test scaffolding, not detections this system ships.

Real detections live in the external DaC repo pointed at by
`config/detections.yaml`. See [detections/README.md](../../../detections/README.md).
