"""Destination adapters. Adding a destination TYPE is one function here;
adding an INSTANCE is pure config (config/destinations.yaml).

Kinds supported: mock (test), webhook (generic), torq (Torq case).
Secrets (tokens) are resolved from env (Key Vault references), never inlined.
"""
import logging
import os
import yaml
import requests

log = logging.getLogger("pyre.dispatch")


class Dispatcher:
    def __init__(self, destinations_path: str):
        self._dests = {}
        if os.path.exists(destinations_path):
            cfg = yaml.safe_load(open(destinations_path)) or {}
            for d in cfg.get("destinations", []):
                if d.get("enabled", True):
                    self._dests[d["name"]] = d

    def send(self, alert, routes: list[str]) -> None:
        for name in routes:
            dest = self._dests.get(name)
            if not dest:
                continue
            kind = dest["kind"]
            if kind == "mock":
                self._mock(dest, alert)
            elif kind == "webhook":
                self._webhook(dest, alert)
            elif kind == "torq":
                self._torq(dest, alert)

    def _payload(self, alert) -> dict:
        return {
            "alert_id": alert.alert_id, "detection_id": alert.detection_id,
            "title": alert.title, "severity": alert.severity,
            "dedup": alert.dedup_string, "context": alert.context,
            "event_count": alert.event_count, "first_event_time": alert.first_event_time,
        }

    def _mock(self, dest, alert):
        # Fire-and-forget to the mock destination Function (test lab).
        url = os.environ.get(dest.get("url_env", ""), dest.get("url", ""))
        if url:
            requests.post(url, json=self._payload(alert), timeout=5)

    def _webhook(self, dest, alert):
        url = os.environ.get(dest.get("url_env", ""), dest.get("url", ""))
        requests.post(url, json=self._payload(alert), timeout=5)

    def _torq(self, dest, alert):
        token = os.environ.get(dest["token_env"])  # Key Vault reference
        if not token:
            log.error("torq dispatch skipped for alert %s: token env %s is unset",
                      alert.alert_id, dest["token_env"])
            return
        url = os.environ.get(dest.get("url_env", ""), dest.get("url", ""))
        requests.post(url, json=self._payload(alert),
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
