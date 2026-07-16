"""Destination adapters. Adding a destination TYPE is one function here;
adding an INSTANCE is pure config (config/destinations.yaml).

Kinds supported: mock (test), webhook (generic), torq (Torq case).
Secrets (tokens) are resolved from env (Key Vault references), never inlined.
"""
import logging
import os
import yaml
import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("pyre.dispatch")

# Pooled + keep-alive, same reason as signals.py: a bare requests.post() builds a
# throwaway Session and pays a fresh TCP connect + TLS handshake every time. An
# alert storm to Torq would otherwise handshake once per alert.
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0))
_SESSION.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0))


class DispatchError(Exception):
    """An alert was not delivered to at least one of its destinations."""


class Dispatcher:
    def __init__(self, destinations_path: str):
        self._dests = {}
        if os.path.exists(destinations_path):
            # Binary mode lets PyYAML do the decode (UTF-8 per spec, BOM-aware)
            # instead of text mode's platform locale encoding.
            with open(destinations_path, "rb") as fh:
                cfg = yaml.safe_load(fh) or {}
            for d in cfg.get("destinations", []):
                if d.get("enabled", True):
                    self._dests[d["name"]] = d

    def send(self, alert, routes: list[str]) -> None:
        """Deliver an alert to every route. Raises DispatchError if ANY failed.

        Every failure mode here used to be silent. requests doesn't raise on 5xx,
        so a Torq outage returned a Response object and the alert was simply
        never delivered - while Redis still held the alert marker saying it had
        been. An unknown route name (a typo in a detection's destinations()) hit
        a bare `continue` and did the same thing. For a security tool "the page
        never went out and nobody knew" is the worst possible outcome, so a
        failure is now loud and the caller can re-open the alert window.
        """
        failed = []
        for name in routes:
            dest = self._dests.get(name)
            if not dest:
                # Not an exception: the other routes should still get the alert.
                # But it must not be invisible - this is a misrouted alert.
                log.error("alert %s (%s): destination '%s' is not configured or is "
                          "disabled in destinations.yaml; nothing was sent to it",
                          alert.alert_id, alert.detection_id, name)
                failed.append(name)
                continue
            try:
                self._SENDERS[dest["kind"]](self, dest, alert)
            except Exception as e:
                log.error("alert %s (%s): delivery to '%s' failed: %s",
                          alert.alert_id, alert.detection_id, name, e)
                failed.append(name)
        if failed:
            raise DispatchError(f"alert {alert.alert_id} undelivered to: {', '.join(failed)}")

    def _alert_payload(self, alert) -> dict:
        # The payload EVERY destination receives, not just one vendor. Carries the
        # full context so a case-creating destination has everything it needs: flat
        # alert metadata (rule id, severity, alert_context, counts) plus the raw
        # triggering event and its p_ fields.
        return {
            "alert_id": alert.alert_id,
            "rule_id": alert.detection_id,
            "title": alert.title,
            "severity": alert.severity,
            "dedup": alert.dedup_string,
            "alert_context": alert.context,
            "event_count": alert.event_count,
            "first_event_time": alert.first_event_time,
            "p_fields": alert.p_fields,
            "event": alert.event,
        }

    def _mock(self, dest, alert):
        # Fire-and-forget to the mock destination Function (test lab).
        url = os.environ.get(dest.get("url_env", ""), dest.get("url", ""))
        if url:
            _SESSION.post(url, json=self._alert_payload(alert), timeout=5).raise_for_status()

    def _webhook(self, dest, alert):
        url = os.environ.get(dest.get("url_env", ""), dest.get("url", ""))
        _SESSION.post(url, json=self._alert_payload(alert), timeout=5).raise_for_status()

    def _torq(self, dest, alert):
        token = os.environ.get(dest["token_env"])  # Key Vault reference
        if not token:
            # Raise rather than return: a missing token means this alert is NOT
            # delivered, and send() has to know that to keep the alert re-firable.
            raise DispatchError(f"token env {dest['token_env']} is unset")
        url = os.environ.get(dest.get("url_env", ""), dest.get("url", ""))
        _SESSION.post(url, json=self._alert_payload(alert),
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=10).raise_for_status()

    # Kind -> sender. A dict, not an if/elif chain, so an unknown kind raises
    # KeyError into send()'s handler instead of silently sending nothing.
    _SENDERS = {"mock": _mock, "webhook": _webhook, "torq": _torq}
