"""
HTML report generator -- self-contained dashboard from finalized pipeline data.

Ported from MiroThinker. Generates a single HTML file with KPI cards,
phase timeline, tool breakdown, and event stream.
"""

from __future__ import annotations

import html
import json
from typing import Any


def generate_dashboard_html(data: dict) -> str:
    """Generate a self-contained HTML dashboard from pipeline data.

    Args:
        data: Finalized pipeline data dict from PipelineCollector.finalize().

    Returns:
        Complete HTML string.
    """
    run_id = html.escape(str(data.get("run_id", "unknown")))
    topic = html.escape(str(data.get("topic", "Unknown Topic")))
    status = html.escape(str(data.get("status", "unknown")))
    elapsed = data.get("elapsed_sec", 0)
    phases = data.get("phases", [])
    tools = data.get("tools", [])
    llm_calls = data.get("llm_calls", [])
    events = data.get("events", [])

    # KPI calculations
    total_tools = len(tools)
    total_llm = len(llm_calls)
    total_phases = len(phases)
    completed_phases = sum(1 for p in phases if p.get("status") == "completed")

    # Tool breakdown
    tool_summary: dict[str, dict] = {}
    for t in tools:
        name = t.get("tool", "unknown")
        if name not in tool_summary:
            tool_summary[name] = {"count": 0, "total_duration": 0, "total_chars": 0}
        tool_summary[name]["count"] += 1
        tool_summary[name]["total_duration"] += t.get("duration", 0)
        tool_summary[name]["total_chars"] += t.get("result_chars", 0)

    tool_rows = ""
    for name, stats in sorted(tool_summary.items(), key=lambda x: -x[1]["count"]):
        avg_dur = stats["total_duration"] / max(stats["count"], 1)
        tool_rows += f"""
        <tr>
            <td>{html.escape(name)}</td>
            <td>{stats['count']}</td>
            <td>{avg_dur:.2f}s</td>
            <td>{stats['total_chars']:,}</td>
        </tr>"""

    # Phase timeline
    phase_rows = ""
    for p in phases:
        status_class = "completed" if p.get("status") == "completed" else "running"
        phase_rows += f"""
        <tr class="{status_class}">
            <td>{html.escape(p.get('name', ''))}</td>
            <td>{html.escape(p.get('status', ''))}</td>
            <td>{p.get('duration', 0):.1f}s</td>
        </tr>"""

    # Event stream (last 50)
    event_items = ""
    for e in events[-50:]:
        etype = e.get("type", "")
        event_items += f'<div class="event event-{etype}">{json.dumps(e)}</div>\n'

    status_color = "#4caf50" if status == "completed" else "#ff9800"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Documentary Pipeline Report — {topic}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           margin: 0; padding: 20px; background: #1a1a2e; color: #e0e0e0; }}
    h1 {{ color: #e94560; margin-bottom: 5px; }}
    h2 {{ color: #0f3460; background: #16213e; padding: 10px; border-radius: 6px; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                 gap: 15px; margin: 20px 0; }}
    .kpi {{ background: #16213e; border-radius: 8px; padding: 15px; text-align: center; }}
    .kpi .value {{ font-size: 2em; font-weight: bold; color: #e94560; }}
    .kpi .label {{ color: #888; font-size: 0.9em; }}
    table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
    th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }}
    th {{ background: #16213e; color: #e94560; }}
    tr.completed td {{ color: #4caf50; }}
    tr.running td {{ color: #ff9800; }}
    .event {{ font-family: monospace; font-size: 0.8em; padding: 4px 8px;
              border-left: 3px solid #333; margin: 2px 0; }}
    .event-phase_start {{ border-color: #4caf50; }}
    .event-phase_end {{ border-color: #2196f3; }}
    .event-tool_start {{ border-color: #ff9800; }}
    .event-tool_end {{ border-color: #9c27b0; }}
    .event-force_end {{ border-color: #f44336; }}
    .status {{ display: inline-block; padding: 4px 12px; border-radius: 12px;
               background: {status_color}; color: white; font-weight: bold; }}
</style>
</head>
<body>
<h1>Documentary Pipeline Report</h1>
<p>Topic: <strong>{topic}</strong> | Run: <code>{run_id}</code> |
   Status: <span class="status">{status}</span></p>

<div class="kpi-grid">
    <div class="kpi"><div class="value">{elapsed:.0f}s</div><div class="label">Total Time</div></div>
    <div class="kpi"><div class="value">{completed_phases}/{total_phases}</div><div class="label">Phases</div></div>
    <div class="kpi"><div class="value">{total_tools}</div><div class="label">Tool Calls</div></div>
    <div class="kpi"><div class="value">{total_llm}</div><div class="label">LLM Calls</div></div>
</div>

<h2>Phase Timeline</h2>
<table>
<tr><th>Phase</th><th>Status</th><th>Duration</th></tr>
{phase_rows}
</table>

<h2>Tool Breakdown</h2>
<table>
<tr><th>Tool</th><th>Calls</th><th>Avg Duration</th><th>Total Result Chars</th></tr>
{tool_rows}
</table>

<h2>Event Stream (last 50)</h2>
{event_items}

</body>
</html>"""
