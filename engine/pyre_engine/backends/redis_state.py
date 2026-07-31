"""Production state backend: Azure Cache for Redis over TLS, authenticated with
Entra (no password in config).

Functions are stateless and run many instances concurrently, so every stateful
behaviour - dedup windows, thresholds, unique() counts, the storm limiter, the
redelivery guard - needs an atomic, low-latency, SHARED store. That is the one
thing the POC's in-process backend cannot provide, and the only reason this
module exists.

Nothing above `StateStore` knows which of the two is in use.

This module is imported ONLY when STATE_BACKEND=redis (see backends/__init__.py),
so a POC deployment never loads redis or azure-identity.
"""
import time

import redis
from azure.identity import DefaultAzureCredential
from redis.credentials import CredentialProvider


class EntraCredentialProvider(CredentialProvider):
    """Fetches a fresh Entra token whenever redis-py opens a NEW connection (pool
    growth, or a reconnect after a network blip).

    The naive alternative - grab a token once at cold start and use it as a fixed
    password - silently starts failing every Redis call once that token expires,
    which is exactly the failure a long-warm worker under sustained load hits."""

    def __init__(self):
        self._cred = DefaultAzureCredential()
        self._token = None

    def get_credentials(self):
        if self._token is None or self._token.expires_on - time.time() < 300:
            self._token = self._cred.get_token("https://redis.azure.com/.default")
        return "", self._token.token


def build_client(cfg):
    """A redis-py client configured from app settings."""
    if not cfg.redis_host:
        raise ValueError("STATE_BACKEND=redis but REDIS_HOST is not set")
    kwargs = dict(host=cfg.redis_host, port=cfg.redis_port, ssl=True, decode_responses=True)
    if cfg.redis_use_entra:
        kwargs["credential_provider"] = EntraCredentialProvider()
    return redis.Redis(**kwargs)
