"""`agentis-top` — terminal dashboard for the adapter.

Standalone Textual process: polls read-only status endpoints on the adapter
(`/status`, `/log`, `/runs/<run_id>/log`) and displays the WebSocket connection
state to Agentis, running tasks with elapsed time, per-run activity log, and
summary statistics since adapter start. Read-only — does not affect the adapter.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any

import requests
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, RichLog, Static

#: Number of finished runs to show below active ones.
_FINISHED_ROWS_LIMIT = 20

_STATUS_ICONS = {
    "running": "▶",
    "success": "✓",
    "failed": "✗",
    "aborted": "⊘",
}

_WS_STATES = {
    "connected": ("●", "green"),
    "connecting": ("◐", "yellow"),
    "disconnected": ("○", "red"),
}


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _format_time(iso_timestamp: str | None) -> str:
    if not iso_timestamp:
        return "–"
    try:
        return datetime.fromisoformat(iso_timestamp).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso_timestamp


class AgentisTopApp(App):
    """Read-only dashboard over adapter status endpoints."""

    TITLE = "agentis-top"

    CSS = """
    #header {
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    #runs {
        height: 1fr;
    }
    #log-title {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }
    #log {
        height: 1fr;
        border-top: solid $surface;
    }
    #stats {
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("g", "toggle_global_log", "Global/run log"),
        ("f", "toggle_follow", "Follow"),
    ]

    def __init__(self, base_url: str, interval: float = 1.0) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self.selected_run_id: str | None = None
        self.show_global_log = False
        self.follow = True
        # (source, seq) cursor for incremental log reads; source is "global" or a run_id.
        self._log_source: str | None = None
        self._log_after = 0

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("connecting…", id="header", markup=True)
        yield DataTable(id="runs")
        yield Static("Log", id="log-title", markup=True)
        yield RichLog(id="log", wrap=False, highlight=False, markup=False, max_lines=2000)
        yield Static("", id="stats", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("TASK", "TITLE", "TYPE", "STATUS", "RECEIVED", "ELAPSED", "ACTIVITY")
        self.set_interval(self.interval, self.refresh_data)
        self.refresh_data()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True)
    def refresh_data(self) -> None:
        source = "global" if self.show_global_log or not self.selected_run_id else self.selected_run_id
        after = self._log_after if source == self._log_source else 0
        try:
            status = requests.get(f"{self.base_url}/status", timeout=3).json()
            if source == "global":
                log_response = requests.get(f"{self.base_url}/log", params={"after": after}, timeout=3)
            else:
                log_response = requests.get(f"{self.base_url}/runs/{source}/log", params={"after": after}, timeout=3)
            entries = log_response.json().get("entries", []) if log_response.ok else []
        except requests.RequestException as exc:
            self.call_from_thread(self._apply_error, str(exc))
            return
        self.call_from_thread(self._apply, status, source, entries)

    def _apply_error(self, error: str) -> None:
        self.query_one("#header", Static).update(
            f"[red]○ adapter unavailable[/red] [dim]{self.base_url} — {error}[/dim]"
        )

    def _apply(self, status: dict[str, Any], source: str, entries: list[dict[str, Any]]) -> None:
        self._render_header(status)
        self._render_runs(status)
        self._render_stats(status)
        self._render_log(source, entries)

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _render_header(self, status: dict[str, Any]) -> None:
        ws = status.get("websocket") or {}
        state = str(ws.get("state") or "disconnected")
        icon, color = _WS_STATES.get(state, ("○", "red"))
        endpoint = ws.get("endpoint") or "?"
        since = _format_time(ws.get("since"))
        attempt = f" attempt {ws['attempt']}" if state == "connecting" and ws.get("attempt") else ""
        error = f" [dim]{ws['last_error']}[/dim]" if state == "disconnected" and ws.get("last_error") else ""
        uptime = _format_duration(status.get("uptime_seconds"))
        adapter = status.get("adapter") or "?"
        adapter_id = status.get("adapter_id")
        adapter_label = f"{adapter} ({adapter_id})" if adapter_id else adapter
        self.query_one("#header", Static).update(
            f"adapter: [bold]{adapter_label}[/bold]"
            f"  •  Agentis WS: [{color}]{icon} {state}[/{color}]{attempt} [dim]{endpoint}[/dim] since {since}{error}"
            f"  •  uptime {uptime}"
        )

    def _render_runs(self, status: dict[str, Any]) -> None:
        runs = status.get("runs") or {}
        rows = list(runs.get("running") or []) + list(runs.get("finished") or [])[:_FINISHED_ROWS_LIMIT]

        table = self.query_one(DataTable)
        previous_selection = self.selected_run_id
        table.clear()
        for run in rows:
            task = f"#{run['task_number']}" if run.get("task_number") is not None else run.get("task_id") or "?"
            kind = run.get("workflow") or run.get("kind") or "?"
            run_status = str(run.get("status") or "?")
            icon = _STATUS_ICONS.get(run_status, "?")
            table.add_row(
                task,
                (run.get("title") or "")[:48],
                kind,
                f"{icon} {run_status}",
                _format_time(run.get("received_at")),
                _format_duration(run.get("duration_seconds")),
                run.get("last_activity") or "",
                key=run["run_id"],
            )

        row_keys = [run["run_id"] for run in rows]
        if previous_selection in row_keys:
            table.move_cursor(row=row_keys.index(previous_selection))
        elif rows:
            self.selected_run_id = row_keys[0]
            table.move_cursor(row=0)
        else:
            self.selected_run_id = None

    def _render_stats(self, status: dict[str, Any]) -> None:
        stats = status.get("stats") or {}
        avg = _format_duration(stats.get("avg_run_duration_seconds"))
        self.query_one("#stats", Static).update(
            f"runs: {stats.get('runs_received', 0)}"
            f" ([green]✓{stats.get('runs_succeeded', 0)}[/green]"
            f" [red]✗{stats.get('runs_failed', 0)}[/red]"
            f" ⊘{stats.get('runs_aborted', 0)}"
            f" [yellow]▶{stats.get('runs_running', 0)}[/yellow])"
            f"  •  messages: {stats.get('messages_received', 0)}"
            f"  •  aborts: {stats.get('aborts_received', 0)}"
            f"  •  avg run: {avg}"
            f"  •  WS reconnects: {stats.get('ws_reconnects', 0)}"
        )

    def _render_log(self, source: str, entries: list[dict[str, Any]]) -> None:
        log = self.query_one(RichLog)
        if source != self._log_source:
            log.clear()
            self._log_source = source
            self._log_after = 0
            title = "Adapter log" if source == "global" else f"Run activity  {source}"
            self.query_one("#log-title", Static).update(f"{title}  [dim]\\[g] toggle  \\[f] follow[/dim]")
        for entry in entries:
            timestamp = _format_time(entry.get("timestamp"))
            if "level" in entry:
                fields = entry.get("fields") or {}
                suffix = " " + " ".join(f"{key}={value}" for key, value in fields.items()) if fields else ""
                log.write(f"{timestamp} {entry['level']:<5} {entry.get('message', '')}{suffix}")
            else:
                log.write(f"{timestamp} {entry.get('text', '')}")
            self._log_after = max(self._log_after, int(entry.get("seq") or 0))
        if entries and self.follow:
            log.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        self.selected_run_id = event.row_key.value

    def action_toggle_global_log(self) -> None:
        self.show_global_log = not self.show_global_log
        self.refresh_data()

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentis-top", description="Terminal dashboard for the agentis adapter.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8001",
        help="Base URL of the adapter status server (default: http://127.0.0.1:8001).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0).",
    )
    args = parser.parse_args()
    AgentisTopApp(base_url=args.url, interval=args.interval).run()


if __name__ == "__main__":
    main()
