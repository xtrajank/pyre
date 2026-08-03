"""Everything the engine needs to know, and nothing else.

There are exactly two kinds of setting, split by what varies:

  * PER SOURCE - which namespace a hub lives in, which field routes to
    detections, which field is the event's timestamp, whether records arrive
    wrapped in an envelope. Every namespace and every hub answers these
    differently, and a list of them does not fit in an app setting, so they
    live in `config/sources.yaml` in this repo.

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

# Keys a hub entry may set. Anything else (including `namespace:`, which
# belongs one level up) is a typo, not a feature.
_HUB_KEYS = {"hub", "consumer_group", "log_type_field", "event_time_field", "envelope_field"}


def _slug(s: str) -> str:
    """Azure function names and app-setting names are alnum-and-underscore
    only; this is the one sanitizer both `connection` and `function_name`
    build on, so the two can never drift apart on what counts as "the same"
    namespace or hub."""
    return "".join(c if c.isalnum() else "_" for c in s)


@dataclass
class Source:
    """One log source = one Event Hub, inside one namespace, feeding one
    trigger.

    The defaults are Azure diagnostic settings' own shape, which is what streams
    from any Azure resource: records wrapped in a `records` array, each carrying
    `category` and `time`. A source that looks different overrides only the
    fields that differ.
    """
    hub: str
    # Which Event Hubs namespace this hub lives in. Every hub in the same
    # namespace shares one connection - see `connection` below - so this is the
    # ONLY thing that has to be typed once per namespace rather than once per
    # hub, which is what keeps a typo from silently pointing one hub at the
    # wrong (or a nonexistent) namespace.
    namespace: str = "default"
    # For `check_eventhub_settings` only: what the namespace's app setting is
    # EXPECTED to resolve to, so a mismatch is a named diagnostic instead of a
    # trigger that silently reads from the wrong namespace. Optional.
    fully_qualified_namespace: str = ""
    consumer_group: str = "$Default"
    # THE routing field: its value on each record must equal a detection's
    # `LogTypes:` entry exactly (case-sensitive) or nothing will ever fire.
    log_type_field: str = "category"
    event_time_field: str = "time"
    # Field holding an array of records when one message carries many. Set to ""
    # when one message is one record.
    envelope_field: str = "records"

    @property
    def connection(self) -> str:
        """The app-setting GROUP the trigger resolves at bind time - never a
        secret. Derived from `namespace`, not typed, so it cannot drift from
        it: `EVENTHUB_<NAMESPACE>__fullyQualifiedNamespace` /
        `EVENTHUB_<NAMESPACE>__credential=managedidentity` is the only setting
        one namespace needs, no matter how many hubs read from it."""
        return f"EVENTHUB_{_slug(self.namespace).upper()}"

    @property
    def function_name(self) -> str:
        """Azure keys checkpoints and metrics on this name, so it must be
        stable across deploys and unique within the whole Function App.
        Folding in `namespace` (not just `hub`) is what makes two different
        namespaces reusing the same hub name - very plausible; Azure
        diagnostic settings default to names like `insights-logs-signinlogs`
        everywhere - impossible to collide. `consumer_group` only joins the
        name when it isn't the default, so the common case stays short and the
        documented "two functions on one hub" case still gets two names."""
        name = f"detect_{_slug(self.namespace)}_{_slug(self.hub)}"
        if self.consumer_group != "$Default":
            name += f"_{_slug(self.consumer_group)}"
        return name


def load_sources(path: str | None = None) -> list[Source]:
    """Read config/sources.yaml: namespaces, each holding any number of hubs.
    An unknown key, a missing `namespace:`/`hub:`, a namespace or hub named
    twice, or two sources that would collide on the same Azure function name
    are typos, not features, so they raise here rather than surfacing as a
    cryptic deploy-time failure."""
    path = path or os.environ.get("SOURCES_PATH") or os.path.join(APP_ROOT, "config", "sources.yaml")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    sources: list[Source] = []
    seen_namespaces: dict[str, str] = {}       # lowercased -> as written, for a clear clash message
    for ns_entry in data.get("namespaces") or []:
        namespace = ns_entry.get("namespace")
        if not namespace:
            raise ValueError(f"{path}: every namespace needs a `namespace:`")
        key = namespace.lower()
        if key in seen_namespaces:
            raise ValueError(f"{path}: namespace {namespace!r} is defined twice "
                             f"(clashes with {seen_namespaces[key]!r} - namespace names "
                             f"become an app-setting name, so they must be unique "
                             f"case-insensitively)")
        seen_namespaces[key] = namespace

        unknown_ns_keys = set(ns_entry) - {"namespace", "fully_qualified_namespace", "hubs"}
        if unknown_ns_keys:
            raise ValueError(f"{path}: namespace {namespace!r} has unknown key(s) "
                             f"{sorted(unknown_ns_keys)}; valid keys are "
                             f"{sorted({'namespace', 'fully_qualified_namespace', 'hubs'})}")

        fqdn = ns_entry.get("fully_qualified_namespace", "")
        hubs = ns_entry.get("hubs") or []
        if not hubs:
            raise ValueError(f"{path}: namespace {namespace!r} needs at least one entry under `hubs:`")

        seen_hubs: set[str] = set()
        for hub_entry in hubs:
            unknown = set(hub_entry) - _HUB_KEYS
            if unknown:
                raise ValueError(f"{path}: namespace {namespace!r}, source {hub_entry.get('hub')!r} "
                                 f"has unknown key(s) {sorted(unknown)}; valid keys are "
                                 f"{sorted(_HUB_KEYS)}")
            hub = hub_entry.get("hub")
            if not hub:
                raise ValueError(f"{path}: namespace {namespace!r}: every source needs a `hub:`")
            if hub in seen_hubs:
                raise ValueError(f"{path}: namespace {namespace!r}: hub {hub!r} is listed twice")
            seen_hubs.add(hub)
            sources.append(Source(namespace=namespace, fully_qualified_namespace=fqdn, **hub_entry))

    _reject_duplicate_function_names(path, sources)
    return sources


def _reject_duplicate_function_names(path: str, sources: list[Source]) -> None:
    seen: dict[str, Source] = {}
    for s in sources:
        name = s.function_name
        other = seen.get(name)
        if other is not None:
            raise ValueError(
                f"{path}: {other.namespace}/{other.hub} and {s.namespace}/{s.hub} would both "
                f"become the Azure function {name!r} (same namespace, hub and consumer_group). "
                f"Give one of them its own `consumer_group:`.")
        seen[name] = s


def check_eventhub_settings(sources: list[Source]) -> list[str]:
    """For each distinct namespace, confirm the app setting its trigger will
    resolve at bind time actually exists - the identity-based
    `<connection>__fullyQualifiedNamespace`, or a raw connection string at
    `<connection>`. This is exactly the check that would have turned this
    session's `EventHub account connection string ... does not exist` failure
    into a named line in `/health` instead of a WebJobs error at cold start.
    """
    problems = []
    checked: set[str] = set()
    for s in sources:
        if s.connection in checked:
            continue
        checked.add(s.connection)
        fqdn = _env(f"{s.connection}__fullyQualifiedNamespace")
        conn_string = _env(s.connection)
        if not fqdn and not conn_string:
            problems.append(
                f"namespace {s.namespace!r}: no app setting {s.connection!r} or "
                f"{s.connection}__fullyQualifiedNamespace - its detect_* function(s) will fail to load")
        elif fqdn and s.fully_qualified_namespace and fqdn != s.fully_qualified_namespace:
            problems.append(
                f"namespace {s.namespace!r}: app setting {s.connection}__fullyQualifiedNamespace "
                f"is {fqdn!r} but sources.yaml declares {s.fully_qualified_namespace!r}")
    return problems


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
