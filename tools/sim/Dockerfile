# 3.11 to match infra/modules/function_app/main.tf `runtime_version`. The deps
# gate (engine/pyre_engine/deps.py) resolves a bundle's imports against THIS
# interpreter, so if it drifts from the Function App's the gate starts lying.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /pyre

# Requirements first so a source edit doesn't re-run the (slow) install layer.
COPY engine/requirements.txt /tmp/engine-requirements.txt
COPY tests/requirements.txt /tmp/tests-requirements.txt

# tests/requirements.txt does `-r ../engine/requirements.txt`, which only
# resolves relative to its own location - so lay both out the way the repo does.
RUN mkdir -p /tmp/req/engine /tmp/req/tests \
    && cp /tmp/engine-requirements.txt /tmp/req/engine/requirements.txt \
    && cp /tmp/tests-requirements.txt  /tmp/req/tests/requirements.txt \
    && pip install --no-cache-dir -r /tmp/req/tests/requirements.txt

# git refuses to operate in a tree it thinks someone else owns; the sim clones
# a throwaway DaC repo it creates itself under /tmp.
RUN git config --global --add safe.directory '*' \
    && git config --global user.email "sim@pyre.local" \
    && git config --global user.name "pyre sim"

COPY . /pyre

# The engine package is imported as `pyre_engine`, and the CLI as `pyre_cli`,
# exactly as they are in the Function App and on an operator's machine.
ENV PYTHONPATH=/pyre/engine:/pyre/cli
