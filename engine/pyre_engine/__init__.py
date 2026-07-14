"""pyre_engine — the reusable detection-processor package.

Importable on its own so the same code runs under the Azure Functions host
(engine/function_app.py), a local test runner (tools/testlab/run_local.py), and
a future scheduled-query or Container Apps host — without forking. See
engine/README.md for the file-by-file map.
"""
