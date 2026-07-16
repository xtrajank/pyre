"""Sim fixtures: a real Redis 6.0, a real HTTP sink, and a real git DaC repo.

Nothing here is a stub of pyre itself. The Processor, Registry, StateStore,
Dispatcher, SignalWriter and the `pyre` CLI are all the shipping code; only the
things OUTSIDE the repo's boundary are stood up locally:

    Azure Cache for Redis  -> a real redis:6.0 container (same major version)
    Cribl HTTP source      -> a local HTTP server that records what arrives
    Torq / mock dest       -> the same local HTTP server
    the external DaC repo  -> a real git repo created under /tmp

That boundary matters. The engine's contract with Redis is "a Redis 6.0 server",
not "fakeredis", and those differ in ways that only show up in production - see
tests/requirements.txt.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import redis as redis_lib

REPO = "/pyre"
sys.path.insert(0, os.path.join(REPO, "engine"))
sys.path.insert(0, os.path.join(REPO, "cli"))


# --- the Cribl lake + the alert destination, as one recording server ----------

class _Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "null")
        cap = self.server.captured
        # The sink can be told to fail, so the dispatch-failure and
        # signals-outage paths are exercised against a real HTTP round trip
        # rather than a monkeypatched exception.
        status = self.server.fail_status.get(self._bucket(), 200)
        if self.path.endswith("/signals"):
            if status == 200:
                for rec in (body if isinstance(body, list) else [body]):
                    key = "alert_records" if rec.get("_dataset") == "pyre_alerts" else "signals"
                    cap[key].append(rec)
        else:
            if status == 200:
                cap["dispatched"].append(body)
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok" if status == 200 else b"boom")

    def _bucket(self):
        return "signals" if self.path.endswith("/signals") else "dispatch"

    def log_message(self, *a):
        pass


class Sink:
    """Records what the engine sent, and can be made to fail on demand."""

    def __init__(self):
        self._srv = HTTPServer(("127.0.0.1", 0), _Sink)
        self._srv.captured = {"signals": [], "alert_records": [], "dispatched": []}
        self._srv.fail_status = {}
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.port = self._srv.server_address[1]

    @property
    def signals(self):
        return self._srv.captured["signals"]

    @property
    def alert_records(self):
        return self._srv.captured["alert_records"]

    @property
    def dispatched(self):
        return self._srv.captured["dispatched"]

    def fail(self, bucket: str, status: int = 503):
        self._srv.fail_status[bucket] = status

    def heal(self, bucket: str):
        self._srv.fail_status.pop(bucket, None)

    def reset(self):
        for v in self._srv.captured.values():
            v.clear()
        self._srv.fail_status.clear()

    @property
    def signals_url(self):
        return f"http://127.0.0.1:{self.port}/signals"

    @property
    def dest_url(self):
        return f"http://127.0.0.1:{self.port}/alert"


@pytest.fixture(scope="session")
def sink():
    return Sink()


# --- a real Redis 6.0 ---------------------------------------------------------

@pytest.fixture(scope="session")
def redis_client():
    c = redis_lib.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
    )
    c.ping()
    info = c.info("server")
    # Guard the premise of this whole environment. If someone bumps the image to
    # 7.x, the Redis-6.0-only checks below keep passing while proving nothing.
    assert info["redis_version"].startswith("6."), (
        f"the sim must run Redis 6.x to mirror Azure Cache for Redis; got "
        f"{info['redis_version']}"
    )
    return c


@pytest.fixture(autouse=True)
def _clean_redis(redis_client):
    redis_client.flushall()
    yield
    redis_client.flushall()


@pytest.fixture
def state(redis_client):
    from pyre_engine.dedup import StateStore
    # Injected client, same seam tests/testlab use. StateStore hardcodes ssl=True
    # on the connection it builds itself, so the TLS/Entra handshake is the one
    # part of this file's path the sim does not cover.
    return StateStore("", 0, use_entra=False, client=redis_client)


# --- the external DaC repo, as a real git repo --------------------------------

RULE_PALO = '''
from pyre_shared import is_high_risk_port


def rule(event):
    return event.get("action") == "allow" and is_high_risk_port(event.get("dport"))


def title(event):
    return f"Palo: allow to high-risk port {event.get('dport')} from {event.get('src_ip')}"


def dedup(event):
    return f"{event.get('src_ip')}:{event.get('dport')}"


def severity(event):
    return "HIGH" if int(event.get("dport", 0)) == 3389 else "MEDIUM"


def alert_context(event):
    return {"src_ip": event.get("src_ip"), "dport": event.get("dport"), "app": event.get("app")}
'''

META_PALO = """
AnalysisType: rule
RuleID: palo_high_risk_port
Filename: palo_high_risk_port.py
Enabled: true
LogTypes: [SEC_Network_Palo_Alto_Traffic]
Severity: Medium
Threshold: 1
DedupPeriodMinutes: 60
CreateAlert: true
"""

RULE_CF = '''
# Records every event it is asked about, so a test can prove routing kept Palo
# events away from it. A detection that never runs writes nothing here.
SEEN = []


def rule(event):
    SEEN.append(event.get("uri", ""))
    return "union select" in str(event.get("uri", "")).lower()


def title(event):
    return f"Cloudflare: SQLi attempt against {event.get('uri')}"
'''

META_CF = """
AnalysisType: rule
RuleID: cloudflare_sqli
Filename: cloudflare_sqli.py
Enabled: true
LogTypes: [SEC_Web_Cloudflare]
Severity: High
Threshold: 1
DedupPeriodMinutes: 60
CreateAlert: true
"""

HELPER = '''
HIGH_RISK_PORTS = {23, 445, 1433, 3306, 3389, 5900}


def is_high_risk_port(port) -> bool:
    try:
        return int(port) in HIGH_RISK_PORTS
    except (TypeError, ValueError):
        return False
'''

META_HELPER = """
AnalysisType: global
GlobalID: pyre_shared
Filename: pyre_shared.py
"""


def _run(cmd, cwd, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


@pytest.fixture(scope="session")
def dac_origin(tmp_path_factory):
    """A real git repo standing in for the external DaC repo, laid out exactly
    like panther-analysis: detections under rules/, helpers in a sibling
    global_helpers/ that `pyre pull` has to copy in separately."""
    origin = tmp_path_factory.mktemp("dac-origin")
    rules = origin / "rules" / "network"
    rules.mkdir(parents=True)
    (rules / "palo_high_risk_port.py").write_text(RULE_PALO)
    (rules / "palo_high_risk_port.yml").write_text(META_PALO)

    web = origin / "rules" / "web"
    web.mkdir(parents=True)
    (web / "cloudflare_sqli.py").write_text(RULE_CF)
    (web / "cloudflare_sqli.yml").write_text(META_CF)

    helpers = origin / "global_helpers"
    helpers.mkdir()
    (helpers / "pyre_shared.py").write_text(HELPER)
    (helpers / "pyre_shared.yml").write_text(META_HELPER)

    _run(["git", "init", "-b", "main", "."], cwd=origin)
    _run(["git", "add", "-A"], cwd=origin)
    _run(["git", "commit", "-m", "initial detections"], cwd=origin)
    return origin
