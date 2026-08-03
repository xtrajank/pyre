"""The event object a detection's `rule(event)` receives.

It IS a dict - every nested value stays a plain dict/list exactly as parsed, so
`isinstance(event, dict)` and `isinstance(event["x"], list)` both work - with two
conveniences layered on top for Panther-style detections.
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
        """Read a lookup-table match out of `p_enrichment[table][key]`.

        pyre does not run a lookup-table store of its own. Whatever attaches
        `p_enrichment` upstream (a normalizer such as Cribl, or the producer
        itself) is the source of truth; this just reads it. A table that was
        never attached reads as a miss, same as an empty lookup.
        """
        return self.deep_get("p_enrichment", table, key)
