"""Small data shapes shared across the engine."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Signal:
    detection_id: str
    log_type: str
    dedup_string: str
    event_time: str
    event_ref: dict[str, Any]
    p_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class Alert:
    alert_id: str
    detection_id: str
    title: str
    severity: str
    dedup_string: str
    context: dict[str, Any] = field(default_factory=dict)
    destinations: list[str] = field(default_factory=list)
    first_event_time: str = ""
    event_count: int = 1
