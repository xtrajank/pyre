"""Enrichment (Lookup Table equivalent) -> populates event['p_enrichment'].

Optional module. Mirrors Panther applying LUT matches before running rules.
For v1 you may skip this entirely if your detections don't read p_enrichment.
Implement lookups against Redis (fast) or an external store; keep it behind
this interface so detections never talk to the store directly.
"""
class Enricher:
    def __init__(self, state_store=None):
        self._store = state_store

    def enrich(self, event: dict) -> dict:
        # TODO: apply lookup tables keyed by schema/selector; attach to p_enrichment.
        event.setdefault("p_enrichment", {})
        return event
