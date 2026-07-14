"""Renders a local testlab run (signals/alerts/dispatched) as a single
self-contained HTML report - a visual alternative to run_local.py's console
printout, for eyeballing a test run's shape (severity mix, what fired, what
got deduped away) instead of reading log lines.

No dependencies beyond the stdlib; the report is one static file you can open
directly in a browser or attach to a PR/ticket.
"""
import html
from datetime import datetime, timezone

# status color per severity - dot + label together (never color alone), per
# the palette's status-color rule.
_SEVERITY_COLOR = {
    "CRITICAL": "#d03b3b",
    "HIGH": "#ec835a",
    "MEDIUM": "#fab219",
    "LOW": "#0ca30c",
    "INFO": "#898781",
}

_STYLE = """
:root {
  --surface-1: #fcfcfb; --page-plane: #f9f9f7; --text-primary:#0b0b0b;
  --text-secondary:#52514e; --muted:#898781; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --grid:#e1e0d9;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    --surface-1:#1a1a19; --page-plane:#0d0d0d; --text-primary:#ffffff;
    --text-secondary:#c3c2b7; --muted:#898781; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --grid:#2c2c2a;
  }
}
:root[data-theme="dark"] {
  --surface-1:#1a1a19; --page-plane:#0d0d0d; --text-primary:#ffffff;
  --text-secondary:#c3c2b7; --muted:#898781; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5; --grid:#2c2c2a;
}
* { box-sizing: border-box; }
body {
  margin:0; padding:32px; background:var(--page-plane); color:var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; }
.hdr { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:24px; }
.hdr h1 { font-size:20px; margin:0; }
.hdr .meta { color:var(--text-secondary); font-size:13px; }
.theme-toggle {
  border:1px solid var(--border); background:var(--surface-1); color:var(--text-primary);
  border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer;
}
.tiles { display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; margin-bottom:32px; }
.stat-tile {
  background:var(--surface-1); border:1px solid var(--border); border-radius:8px;
  padding:16px 18px;
}
.stat-value { font-size:30px; font-weight:600; line-height:1.1; }
.stat-label {
  font-size:12px; color:var(--text-secondary); text-transform:uppercase;
  letter-spacing:.04em; margin-top:4px;
}
section { margin-bottom:32px; }
section h2 {
  font-size:14px; text-transform:uppercase; letter-spacing:.04em;
  color:var(--text-secondary); margin:0 0 10px;
}
table { width:100%; border-collapse:collapse; background:var(--surface-1); border-radius:8px; overflow:hidden; border:1px solid var(--border); }
th {
  text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.03em;
  color:var(--text-secondary); border-bottom:1px solid var(--grid); padding:9px 12px;
}
td { padding:9px 12px; border-bottom:1px solid var(--grid); font-size:13px; }
tr:last-child td { border-bottom:none; }
td.empty, tr.empty td { color:var(--muted); font-style:italic; }
.badge { display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:600; }
.dot { width:8px; height:8px; border-radius:50%; flex:none; display:inline-block; }
.foot { color:var(--muted); font-size:12px; margin-top:24px; }
code { background:var(--grid); border-radius:4px; padding:1px 5px; font-size:12px; }
"""

_TOGGLE_SCRIPT = """
(function () {
  var root = document.documentElement;
  var btn = document.getElementById('theme-toggle');
  btn.addEventListener('click', function () {
    var current = root.getAttribute('data-theme') ||
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    root.setAttribute('data-theme', current === 'dark' ? 'light' : 'dark');
  });
})();
"""


def _badge(severity: str) -> str:
    sev = (severity or "INFO").upper()
    color = _SEVERITY_COLOR.get(sev, _SEVERITY_COLOR["INFO"])
    return (f'<span class="badge"><span class="dot" style="background:{color}"></span>'
            f'{html.escape(sev)}</span>')


def _tile(value, label: str) -> str:
    return (f'<div class="stat-tile"><div class="stat-value">{value}</div>'
            f'<div class="stat-label">{html.escape(label)}</div></div>')


def _table(headers, rows_html: str) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{rows_html}</tbody></table>"


def _rows(items, col_fns, empty_msg: str) -> str:
    if not items:
        return f'<tr class="empty"><td colspan="{len(col_fns)}">{html.escape(empty_msg)}</td></tr>'
    out = []
    for it in items:
        cells = "".join(f"<td>{fn(it)}</td>" for fn in col_fns)
        out.append(f"<tr>{cells}</tr>")
    return "".join(out)


def render(meta: dict, captured: dict) -> str:
    """meta: {"bundle": str, "file": str, "event_count": int}
    captured: {"signals": [...], "alert_records": [...], "dispatched": [...]}
    (the same shapes run_local.py already prints to the console)."""
    signals = captured.get("signals", [])
    alerts = captured.get("alert_records", [])
    dispatched = captured.get("dispatched", [])

    signal_rows = _rows(
        signals,
        [
            lambda s: html.escape(str(s.get("detection_id", ""))),
            lambda s: html.escape(str(s.get("p_log_type", ""))),
            lambda s: html.escape(str(s.get("dedup", ""))),
            lambda s: html.escape(str(s.get("p_event_time", ""))),
        ],
        "no detection matched any event in this run",
    )
    alert_rows = _rows(
        alerts,
        [
            lambda a: _badge(a.get("severity")),
            lambda a: html.escape(str(a.get("detection_id", ""))),
            lambda a: html.escape(str(a.get("title", ""))),
            lambda a: html.escape(str(a.get("dedup", ""))),
            lambda a: html.escape(str(a.get("first_event_time", ""))),
        ],
        "no alert fired - matches stayed below threshold, or were grouped by dedup",
    )
    dispatch_rows = _rows(
        dispatched,
        [
            lambda d: _badge(d.get("severity")),
            lambda d: html.escape(str(d.get("detection_id", ""))),
            lambda d: html.escape(str(d.get("title", ""))),
            lambda d: html.escape(str(d.get("alert_id", "")))[:8],
        ],
        "nothing was dispatched to a destination",
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pyre local test run</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <div>
      <h1>pyre local test run</h1>
      <div class="meta">bundle <code>{html.escape(meta.get('bundle', ''))}</code>
        &nbsp;&middot;&nbsp; file <code>{html.escape(meta.get('file', ''))}</code>
        &nbsp;&middot;&nbsp; {generated}</div>
    </div>
    <button id="theme-toggle" class="theme-toggle" type="button">Toggle theme</button>
  </div>

  <div class="tiles">
    {_tile(meta.get('event_count', 0), 'events processed')}
    {_tile(len(signals), 'signals (every match)')}
    {_tile(len(alerts), 'alerts (post threshold/dedup)')}
    {_tile(len(dispatched), 'dispatched to destination')}
  </div>

  <section>
    <h2>Signals &mdash; every rule() == True, before threshold/dedup</h2>
    {_table(["Detection", "Log type", "Dedup string", "Event time"], signal_rows)}
  </section>

  <section>
    <h2>Alerts &mdash; survived threshold, dedup window, and storm limit</h2>
    {_table(["Severity", "Detection", "Title", "Dedup string", "First event time"], alert_rows)}
  </section>

  <section>
    <h2>Dispatched &mdash; sent to the destination (mock in this run)</h2>
    {_table(["Severity", "Detection", "Title", "Alert ID"], dispatch_rows)}
  </section>

  <div class="foot">
    Generated by tools/testlab/run_local.py --html. Signals are the full audit
    trail; alerts are what a human would see; dispatched is what actually left
    the engine (== alerts, routed to the mock destination here, Torq in prod).
  </div>
</div>
<script>{_TOGGLE_SCRIPT}</script>
</body>
</html>
"""
