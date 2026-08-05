"""Everything the engine needs to know, and nothing else.

There are exactly two kinds of setting, split by what varies:

  * PER SOURCE - which namespace a hub lives in, which field routes to
    detections, which field is the event's timestamp, whether records arrive
    wrapped in an envelope. Every namespace and every hub answers these
    differently, and a list of them does not fit in an app setting, so they
    live in `config/sources.yaml` in this repo.

  * PER INSTANCE - where the detection bundle is, where signals go, where
    alerts go, where dedup state lives. Same shape everywhere; only the values
    differ, so they are App settings.

Every "which implementation" decision is a NAMED VALUE, never a presence check:
`DETECTIONS_SOURCE`, `SIGNAL_DESTINATION`, `ALERT_DESTINATION`, `STATE_BACKEND`.
Setting a URL does not silently switch a mode, and two settings can never
disagree about which one wins. What an instance IS - a laptop run, a demo
writing to a blob you can read, a production feed into an external SIEM - is
entirely these values. There is no environment named in this code.

`problems()` is the other half of that: anything contradictory (a destination
selected with nowhere to send it, an unparseable number, no log sources at all)
is reported by /health and logged at startup, rather than surfacing later as a
silently dropped record.

Nothing else is configurable. If you are looking for a knob that isn't here,
it doesn't exist. See docs/configuration.md.
"""
import logging
import os
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("pyre.config")

# The Function App root: the directory holding function_app.py, host.json and
# config/. Anchored to THIS file rather than the working directory, so the same
# code behaves identically under the Azure worker, pytest, or a shell.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Keys a hub entry may set. Anything else (including `namespace:`, which
# belongs one level up) is a typo, not a feature.
_HUB_KEYS = {"hub", "consumer_group", "log_type_field", "event_time_field", "envelope_field"}

# The allowed values of every selector setting, in one place so the validator
# and the docs cannot drift from what the code accepts.
DETECTIONS_SOURCES = ("blob", "local")
DESTINATIONS = ("blob", "http", "none")
STATE_BACKENDS = ("memory", "redis")


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

    @property
    def id(self) -> str:
        """How a source identifies itself on every record it produces, and the
        key `/ingest?source=` accepts."""
        return f"{self.namespace}/{self.hub}"


def sources_path() -> str:
    """Where sources.yaml is read from. `SOURCES_PATH` overrides it; otherwise
    it is anchored to the app root, not the working directory."""
    return _env("SOURCES_PATH") or os.path.join(APP_ROOT, "config", "sources.yaml")


def load_sources(path: str | None = None) -> list[Source]:
    """Read config/sources.yaml: namespaces, each holding any number of hubs.
    An unknown key, a missing `namespace:`/`hub:`, a namespace or hub named
    twice, or two sources that would collide on the same Azure function name
    are typos, not features, so they raise here rather than surfacing as a
    cryptic deploy-time failure."""
    path = path or sources_path()
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

        seen_hubs: set[tuple[str, str]] = set()
        for hub_entry in hubs:
            unknown = set(hub_entry) - _HUB_KEYS
            if unknown:
                raise ValueError(f"{path}: namespace {namespace!r}, source {hub_entry.get('hub')!r} "
                                 f"has unknown key(s) {sorted(unknown)}; valid keys are "
                                 f"{sorted(_HUB_KEYS)}")
            hub = hub_entry.get("hub")
            if not hub:
                raise ValueError(f"{path}: namespace {namespace!r}: every source needs a `hub:`")
            pair = (hub, hub_entry.get("consumer_group", "$Default"))
            if pair in seen_hubs:
                raise ValueError(f"{path}: namespace {namespace!r}: hub {hub!r} is listed twice "
                                 f"on consumer group {pair[1]!r}")
            seen_hubs.add(pair)
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
    `<connection>`. Without this, a namespace whose app setting was never
    created surfaces as a WebJobs error at cold start rather than a named line
    in /health.
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


def identity_state() -> dict:
    """What `DefaultAzureCredential` has to work with, reported next to the
    thing it breaks.

    Everything the engine touches in Azure - the detection bundle, the output
    blobs, Redis - authenticates as the app's managed identity, so "no identity"
    surfaces several unrelated-looking failures at once. It is also the one
    failure the exception text doesn't name: `DefaultAzureCredential failed to
    retrieve a token` reads identically whether the identity is off, or is on
    and pinned to a client id that isn't attached to this app.

    `endpoint` is the platform's own signal: Azure injects IDENTITY_ENDPOINT
    only once an identity is assigned, so False means Settings -> Identity,
    not RBAC. A missing ROLE fails later and differently (a 403 naming the
    action), which is why that isn't checked here.

    `azure_client_id` pins every credential in the app to ONE user-assigned
    identity - correct when the app has several, and a total outage when it
    holds a stale or unattached id. It covers this app's OWN Azure calls only;
    the Event Hub triggers and AzureWebJobsStorage are the HOST's connections
    and take `EVENTHUB_<NS>__clientId` / `AzureWebJobsStorage__clientId`
    separately. Setting one and not the others is the failure where /health
    returns 200 while every trigger stays silent, so both are reported.
    """
    return {
        "endpoint": bool(_env("IDENTITY_ENDPOINT") or _env("MSI_ENDPOINT")),
        "azure_client_id": _env("AZURE_CLIENT_ID") or None,
        "host_connection_client_ids": {
            name: os.environ[name]
            for name in sorted(os.environ)
            if name.endswith("__clientId")
        } or None,
    }


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    """An unparseable number must not take the app down. `int(_env(...))` inside
    a field default runs during the import of function_app.py, so one typo'd app
    setting would raise there and Azure would show an EMPTY FUNCTION LIST with no
    obvious cause. Fall back to the default instead; `problems()` re-checks the
    raw value and reports it."""
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("app setting %s is %r, which is not a whole number; using %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_choice(name: str, choices: tuple[str, ...], default: str) -> str:
    """A selector always resolves to something valid; an unrecognised value
    becomes a `problems()` entry rather than an exception at import."""
    raw = _env(name).lower()
    if not raw:
        return default
    if raw not in choices:
        log.warning("app setting %s is %r; expected one of %s. Using %r.",
                    name, raw, ", ".join(choices), default)
        return default
    return raw


@dataclass
class RuntimeConfig:
    # A free-text label for this instance, echoed by /health so a response can
    # be attributed at a glance. Purely cosmetic - no behaviour reads it.
    instance_label: str = field(default_factory=lambda: _env("INSTANCE_LABEL"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    sources: list[Source] = field(default_factory=load_sources)

    # --- where the detections come from -------------------------------------
    # `blob` pulls the published bundle from Blob via Managed Identity,
    # re-checking every DETECTIONS_REFRESH_SECONDS. `local` reads a directory on
    # disk (tests, tools/run_local.py). The default is `blob` because a deployed
    # instance is the normal case and a missing setting must not quietly read an
    # empty folder.
    detections_source: str = field(
        default_factory=lambda: _env_choice("DETECTIONS_SOURCE", DETECTIONS_SOURCES, "blob"))
    detections_blob_account_url: str = field(
        default_factory=lambda: _env("DETECTIONS_BLOB_ACCOUNT_URL"))
    detections_container: str = field(
        default_factory=lambda: _env("DETECTIONS_CONTAINER", "detections"))
    detections_pointer: str = field(
        default_factory=lambda: _env("DETECTIONS_POINTER", "current.json"))
    detections_local_dir: str = field(
        default_factory=lambda: _env("DETECTIONS_LOCAL_DIR", "./.bundle"))
    detections_refresh_seconds: int = field(
        default_factory=lambda: _env_int("DETECTIONS_REFRESH_SECONDS", 60))

    # --- where signals go ----------------------------------------------------
    # Signals are the audit trail: every match, never deduplicated. High volume,
    # and what you query later. A lake or a blob wants them batched.
    signal_destination: str = field(
        default_factory=lambda: _env_choice("SIGNAL_DESTINATION", DESTINATIONS, "none"))
    signal_blob_account_url: str = field(
        default_factory=lambda: _env("SIGNAL_BLOB_ACCOUNT_URL"))
    signal_blob_container: str = field(
        default_factory=lambda: _env("SIGNAL_BLOB_CONTAINER", "pyre-output"))
    signal_http_url: str = field(default_factory=lambda: _env("SIGNAL_HTTP_URL"))
    signal_http_auth_header: str = field(
        default_factory=lambda: _env("SIGNAL_HTTP_AUTH_HEADER"))
    signal_http_batch: bool = field(
        default_factory=lambda: _env_bool("SIGNAL_HTTP_BATCH", True))

    # --- where alerts go -----------------------------------------------------
    # Alerts are the page: deduplicated, low volume, one per case. A case tool
    # webhook wants one alert per request, which is why the batch default here
    # is the opposite of the signal one.
    alert_destination: str = field(
        default_factory=lambda: _env_choice("ALERT_DESTINATION", DESTINATIONS, "none"))
    alert_blob_account_url: str = field(
        default_factory=lambda: _env("ALERT_BLOB_ACCOUNT_URL"))
    alert_blob_container: str = field(
        default_factory=lambda: _env("ALERT_BLOB_CONTAINER", "pyre-output"))
    alert_http_url: str = field(default_factory=lambda: _env("ALERT_HTTP_URL"))
    alert_http_auth_header: str = field(
        default_factory=lambda: _env("ALERT_HTTP_AUTH_HEADER"))
    alert_http_batch: bool = field(
        default_factory=lambda: _env_bool("ALERT_HTTP_BATCH", False))

    http_timeout_seconds: int = field(
        default_factory=lambda: _env_int("HTTP_TIMEOUT_SECONDS", 10))

    # --- dedup / threshold state --------------------------------------------
    # `redis` shares state across workers, which is what makes thresholds and
    # dedup correct under scale-out. `memory` is per worker and resets on a
    # restart: correct on one instance, not across several.
    state_backend: str = field(
        default_factory=lambda: _env_choice("STATE_BACKEND", STATE_BACKENDS, "memory"))
    redis_host: str = field(default_factory=lambda: _env("REDIS_HOST"))
    redis_port: int = field(default_factory=lambda: _env_int("REDIS_PORT", 6380))
    alert_storm_limit_per_hour: int = field(
        default_factory=lambda: _env_int("ALERT_STORM_LIMIT_PER_HOUR", 1000))

    def problems(self) -> list[str]:
        """Every way this instance's settings contradict themselves, named.

        Reported by /health and logged once at startup. A destination selected
        with nowhere to send it is the important one: without this check it
        looks exactly like a healthy app that happens to produce no output.
        """
        out: list[str] = []

        if not self.sources:
            out.append(f"no log sources: {sources_path()} is missing or empty. Copy "
                       f"config/sources.example.yaml to config/sources.yaml. Until then "
                       f"nothing is ingested - no Event Hub trigger exists.")

        out += _check_choice("DETECTIONS_SOURCE", DETECTIONS_SOURCES, self.detections_source)
        if self.detections_source == "blob" and not self.detections_blob_account_url:
            out.append("DETECTIONS_SOURCE=blob but DETECTIONS_BLOB_ACCOUNT_URL is not set; "
                       "no detections can be loaded")

        for stream in ("signal", "alert"):
            kind = getattr(self, f"{stream}_destination")
            out += _check_choice(f"{stream.upper()}_DESTINATION", DESTINATIONS, kind)
            if kind == "blob" and not getattr(self, f"{stream}_blob_account_url"):
                out.append(f"{stream.upper()}_DESTINATION=blob but "
                           f"{stream.upper()}_BLOB_ACCOUNT_URL is not set; "
                           f"{stream}s will be dropped")
            if kind == "http" and not getattr(self, f"{stream}_http_url"):
                out.append(f"{stream.upper()}_DESTINATION=http but "
                           f"{stream.upper()}_HTTP_URL is not set; {stream}s will be dropped")
        if self.signal_destination == "none" and self.alert_destination == "none":
            out.append("both SIGNAL_DESTINATION and ALERT_DESTINATION are 'none': detections "
                       "run but nothing is written anywhere. See docs/configuring-destinations.md")

        out += _check_choice("STATE_BACKEND", STATE_BACKENDS, self.state_backend)
        if self.state_backend == "redis" and not self.redis_host:
            out.append("STATE_BACKEND=redis but REDIS_HOST is not set")

        for name in ("DETECTIONS_REFRESH_SECONDS", "HTTP_TIMEOUT_SECONDS",
                     "REDIS_PORT", "ALERT_STORM_LIMIT_PER_HOUR"):
            out += _check_int(name)

        out += check_eventhub_settings(self.sources)
        return out


def _check_choice(name: str, choices: tuple[str, ...], resolved: str) -> list[str]:
    """Report the app setting when it is the thing that's wrong, and the resolved
    value when it was set some other way (a test, or tools/run_local.py) - so a
    bad value is named whichever path it arrived by."""
    raw = _env(name).lower()
    if raw and raw not in choices:
        return [f"{name} is {raw!r}; expected one of {', '.join(choices)}"]
    if resolved not in choices:
        return [f"{name} resolved to {resolved!r}; expected one of {', '.join(choices)}"]
    return []


def _check_int(name: str) -> list[str]:
    raw = _env(name)
    if not raw:
        return []
    try:
        int(raw)
    except ValueError:
        return [f"{name} is {raw!r}, which is not a whole number; the default is in use"]
    return []
