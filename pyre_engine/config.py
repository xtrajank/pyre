"""Everything the engine needs to know, and nothing else.

There are exactly two kinds of setting, split by what varies:

  * PER SOURCE - which field routes to detections, which field is the event's
    timestamp, whether records arrive wrapped in an envelope. Every log source
    answers these differently, and a list of sources does not fit in an app
    setting, so they live in `config/sources.yaml` in this repo.

  * PER ENVIRONMENT - where the detection bundle is, where signals and alerts
    go. Same shape in poc/dev/prod, only the values differ, so they are app
    settings in the portal.

Nothing else is configurable. If you are looking for a knob that isn't here,
it doesn't exist.
"""
import os
from dataclasses import dataclass, field

import yaml

# The Function App root: the directory holding function_app.py, host.json and
# config/. Anchored to THIS file rather than the working directory, so the same
# code behaves identically under the Azure worker, pytest, or a shell.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Source:
    """One log source = one Event Hub = one trigger.

    The defaults are Azure diagnostic settings' own shape, which is what streams
    from any Azure resource: records wrapped in a `records` array, each carrying
    `category` and `time`. A source that looks different overrides only the
    fields that differ.
    """
    hub: str
    # App setting holding the Event Hub connection. Sources in a different
    # namespace name a different setting; sources in the same one share it.
    connection: str = "EVENTHUB_CONNECTION"
    consumer_group: str = "$Default"
    # THE routing field: its value on each record must equal a detection's
    # `LogTypes:` entry exactly (case-sensitive) or nothing will ever fire.
    log_type_field: str = "category"
    event_time_field: str = "time"
    # Field holding an array of records when one message carries many. Set to ""
    # when one message is one record.
    envelope_field: str = "records"


def load_sources(path: str | None = None) -> list[Source]:
    """Read config/sources.yaml. An unknown key is a typo, not a feature, so it
    raises here rather than being silently ignored at 3am."""
    path = path or os.environ.get("SOURCES_PATH") or os.path.join(APP_ROOT, "config", "sources.yaml")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    known = set(Source.__dataclass_fields__)
    sources = []
    for entry in data.get("sources") or []:
        unknown = set(entry) - known
        if unknown:
            raise ValueError(f"{path}: source {entry.get('hub')!r} has unknown key(s) "
                             f"{sorted(unknown)}; valid keys are {sorted(known)}")
        if not entry.get("hub"):
            raise ValueError(f"{path}: every source needs a `hub:`")
        sources.append(Source(**entry))
    return sources


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class RuntimeConfig:
    env: str = field(default_factory=lambda: _env("PYRE_ENV", "poc"))
    sources: list[Source] = field(default_factory=load_sources)

    # --- where the detections come from -------------------------------------
    # Set DAC_BLOB_ACCOUNT_URL and the engine pulls the published bundle from
    # Blob via Managed Identity, re-checking every DAC_REFRESH_SECONDS. Leave it
    # empty and it reads DAC_LOCAL_DIR off disk (local runs and tests).
    dac_blob_account_url: str = field(default_factory=lambda: _env("DAC_BLOB_ACCOUNT_URL"))
    dac_container: str = field(default_factory=lambda: _env("DAC_CONTAINER", "detections"))
    dac_pointer: str = field(default_factory=lambda: _env("DAC_POINTER", "current.json"))
    dac_local_dir: str = field(default_factory=lambda: _env("DAC_LOCAL_DIR", "./.bundle"))
    dac_refresh_seconds: int = field(default_factory=lambda: int(_env("DAC_REFRESH_SECONDS", "60")))

    # --- where signals and alerts go ----------------------------------------
    # HTTP wins if both are set: that's the production SIEM/lake endpoint, and
    # the blob is the readable stand-in for it.
    output_http_url: str = field(default_factory=lambda: _env("OUTPUT_HTTP_URL"))
    output_blob_account_url: str = field(default_factory=lambda: _env("OUTPUT_BLOB_ACCOUNT_URL"))
    output_blob_container: str = field(default_factory=lambda: _env("OUTPUT_BLOB_CONTAINER", "pyre-output"))
    # Optional: every alert is ALSO POSTed here (a case tool, Torq, Teams...).
    # Signals never are - they're the audit trail, not a page.
    alert_webhook_url: str = field(default_factory=lambda: _env("ALERT_WEBHOOK_URL"))

    # --- dedup / threshold state --------------------------------------------
    # Set REDIS_HOST and state is shared across workers (production). Leave it
    # empty and state is in-process: correct on one instance, not across
    # scale-out. There is no third option and no mode switch to get wrong.
    redis_host: str = field(default_factory=lambda: _env("REDIS_HOST"))
    redis_port: int = field(default_factory=lambda: int(_env("REDIS_PORT", "6380")))
    storm_limit_per_hour: int = field(default_factory=lambda: int(_env("STORM_LIMIT", "1000")))

    @property
    def state_backend(self) -> str:
        return "redis" if self.redis_host else "memory"
