"""The event object detections receive.

Panther's PantherEvent gives detections `.deep_get()`/`.lookup()` but doesn't
subclass dict, so `isinstance(event, dict)` is always False even though it
behaves like one - a real authoring papercut. Event fixes that by actually
being a dict: every nested value stays a plain dict/list exactly as parsed,
`isinstance(event, dict)` and `isinstance(nested_value, list)` both work, and
`.deep_get()`/`.lookup()` are just two extra methods layered on top.
"""
from typing import Any


class Event(dict):
    def deep_get(self, *keys: str, default: Any = None) -> Any:
        """Walk nested keys in order; return `default` the moment a key is
        missing or a non-dict is hit, instead of raising."""
        cur: Any = self
        for key in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
            if cur is None:
                return default
        return cur

    def lookup(self, table: str, key: str) -> Any:
        """A lookup-table match, read from p_enrichment[table][key].

        pyre does not run its own lookup-table store - Cribl's native Lookup
        feature is the source of truth and is responsible for attaching the
        match under p_enrichment before the event reaches Event Hubs (see
        docs/architecture.md and enrichment.py). This is just a reader over
        whatever Cribl put there; a table/key it hasn't attached yet reads as
        a miss (None), same as a real lookup-table miss.

        Assumed shape: p_enrichment[table] is a dict keyed by the same lookup
        key the detection computes (mirrors Panther's lookup(table, key)
        signature). Revisit this once Cribl's side of the primary-key
        matching is finalized - a table with only one candidate row per event
        may end up attached directly as p_enrichment[table] with no key
        needed, in which case this can drop the key argument.
        """
        return self.deep_get("p_enrichment", table, key)
