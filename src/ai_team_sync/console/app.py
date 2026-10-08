"""Textual application for the read-only Tower Agent Console."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Label, ListItem, ListView, Static

from .sanitize import safe_text
from .sources import ConsoleConfig, ReadOnlySources

EVENT_COLORS = {
    "USER": "bright_white",
    "CLAUDE": "magenta",
    "CODEX": "cyan",
    "ATS": "blue",
    "ECHO": "green",
    "TOOL": "yellow",
    "HANDOFF": "bright_magenta",
    "REVIEW": "bright_cyan",
    "RULING": "bright_blue",
    "BLOCKER": "red",
    "TASK": "bright_yellow",
    "SYSTEM": "white",
}
STATE_COLORS = {
    "OBSERVED": "green",
    "PARTIAL": "yellow",
    "UNAVAILABLE": "magenta",
    "UNINSTRUMENTED": "bright_black",
    "DISCONNECTED": "red",
}


class TowerConsole(App):
    TITLE = "TOWER AGENT CONSOLE"
    SUB_TITLE = "read-only operational exchange"
    CSS = """
    Screen { background: #080c12; color: #d7e0ea; }
    Header { background: #132033; color: #f5f8fa; }
    #connectivity { height: 1; padding: 0 1; background: #101a28; }
    #workspace { height: 1fr; }
    #top { height: 3fr; min-height: 10; }
    #middle { height: 2fr; min-height: 7; }
    #bottom { height: 2fr; min-height: 7; }
    .pane { border: round #344963; padding: 0 1; }
    .pane-title { color: #8bb8ff; text-style: bold; height: 1; }
    #agents-pane { width: 31; min-width: 24; }
    #exchange-pane { width: 1fr; }
    #task-pane { width: 2fr; }
    #detail-pane { width: 3fr; }
    #tools-pane { width: 3fr; }
    #system-pane { width: 2fr; }
    #agent-list, #exchange, #tool-table { height: 1fr; }
    #task, #detail, #system { height: 1fr; overflow-y: auto; }
    #filter { display: none; dock: bottom; height: 3; border: round #5c91d1; }
    #filter.visible { display: block; }
    Screen.narrow #agents-pane, Screen.narrow #task-pane, Screen.narrow #tools-pane {
        display: none;
    }
    Screen.narrow #middle { height: 2fr; }
    Screen.narrow #bottom { height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "pause", "Pause"),
        Binding("f", "filter_type", "Type"),
        Binding("slash", "search", "Search"),
        Binding("t", "task_filter", "Task"),
        Binding("a", "agent_filter", "Agent"),
        Binding("s", "session_filter", "Session"),
        Binding("enter", "expand", "Expand"),
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("f1", "focus_agents", "Agents", show=False),
        Binding("f2", "focus_exchange", "Exchange", show=False),
        Binding("f3", "focus_task", "ATS", show=False),
        Binding("f4", "echo_filter", "Echo", show=False),
        Binding("f5", "focus_task", "Tasks", show=False),
        Binding("f6", "focus_tools", "Tools", show=False),
        Binding("f7", "focus_system", "System", show=False),
    ]

    def __init__(self, config: ConsoleConfig, *, sources: ReadOnlySources | None = None):
        super().__init__()
        self.sources = sources or ReadOnlySources(config)
        self.sessions: list[dict] = []
        self.events: list[dict] = []
        self.event_by_id: dict[str, dict] = {}
        self.cursor: str | None = None
        self.paused = False
        self.follow = True
        self.filters = {"search": "", "task": "", "agent": "", "session": "", "type": ""}
        self.filter_kind = "search"
        self.connectivity = {"ATS": "DISCONNECTED", "Echo": "DISCONNECTED"}
        self.selected_task_id: int | None = None
        self.selected_session_id: str | None = None
        self.task_snapshot: dict[str, Any] | None = None
        self.lineage_truncated = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("ATS DISCONNECTED  Echo DISCONNECTED  follow=ON", id="connectivity")
        with Vertical(id="workspace"):
            with Horizontal(id="top"):
                with Vertical(classes="pane", id="agents-pane"):
                    yield Label("AGENTS / SESSIONS", classes="pane-title")
                    yield ListView(id="agent-list")
                with Vertical(classes="pane", id="exchange-pane"):
                    yield Label("LIVE EXCHANGE", classes="pane-title")
                    yield DataTable(id="exchange", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="middle"):
                with Vertical(classes="pane", id="task-pane"):
                    yield Label("TASK / AUTHORITY", classes="pane-title")
                    yield Static("Select a session or task.", id="task", markup=False)
                with Vertical(classes="pane", id="detail-pane"):
                    yield Label("DETAIL / EXPANDED EVENT", classes="pane-title")
                    yield Static("No event selected.", id="detail", markup=False)
            with Horizontal(id="bottom"):
                with Vertical(classes="pane", id="tools-pane"):
                    yield Label("TOOL TRACE / ECHO TELEMETRY", classes="pane-title")
                    yield DataTable(id="tool-table", cursor_type="row", zebra_stripes=True)
                with Vertical(classes="pane", id="system-pane"):
                    yield Label("SERVICES / GPU / GIT", classes="pane-title")
                    yield Static("Collecting read-only status…", id="system", markup=False)
        yield Input(placeholder="filter", id="filter")
        yield Footer()

    async def on_mount(self) -> None:
        exchange = self.query_one("#exchange", DataTable)
        exchange.add_columns("TIME", "TYPE", "FROM → TO", "SUMMARY", "STATE")
        tools = self.query_one("#tool-table", DataTable)
        tools.add_columns("TOOL", "CALLS", "AVG", "P95", "ERR", "LAST")
        await self.refresh_all()
        self.set_interval(5.0, self.poll_live)
        self.set_interval(15.0, self.refresh_slow)
        exchange.focus()

    async def on_unmount(self) -> None:
        await self.sources.close()

    async def refresh_all(self) -> None:
        await self.refresh_sessions()
        await self.refresh_events(bootstrap=True)
        await self.refresh_echo()
        await self.refresh_system()
        self.render_connectivity()

    async def poll_live(self) -> None:
        if not self.paused:
            await self.refresh_events()

    async def refresh_slow(self) -> None:
        if self.paused:
            return
        await self.refresh_sessions()
        await self.refresh_echo()
        await self.refresh_system()
        if self.selected_task_id:
            await self.refresh_task(self.selected_task_id)
        self.render_connectivity()

    async def refresh_sessions(self) -> None:
        try:
            payload = await self.sources.ats_sessions()
            self.sessions = payload.get("sessions", [])
            self.lineage_truncated = bool(payload.get("lineage_truncated"))
            self.connectivity["ATS"] = "PARTIAL" if self.lineage_truncated else "OBSERVED"
        except Exception:
            self.connectivity["ATS"] = "DISCONNECTED"
            return
        view = self.query_one("#agent-list", ListView)
        await view.clear()
        for row in self.sessions:
            health = row.get("ats_health", {})
            marks = "ATS✓" if health.get("registered") else "ATS!"
            if not health.get("scoped"):
                marks += " scope—"
            task = f" #{row['task_id']}" if row.get("task_id") else " task—"
            parent = " ↳" if row.get("parent_session_id") else ""
            label = Text()
            label.append(parent)
            agent = safe_text(row.get("agent"), 22)
            label.append(agent, style=self._agent_color(agent))
            label.append(f"\n  {row.get('status')} {marks}{task} {row.get('effective_mode')}")
            item = ListItem(Label(label), name=row.get("id"))
            item.id = f"session-{row.get('id')}"
            await view.append(item)

    async def refresh_events(self, bootstrap: bool = False) -> None:
        try:
            payload = await self.sources.ats_events(cursor=None if bootstrap else self.cursor)
            self.connectivity["ATS"] = "PARTIAL" if self.lineage_truncated else "OBSERVED"
        except Exception:
            self.connectivity["ATS"] = "DISCONNECTED"
            self.render_connectivity()
            return
        self.cursor = payload.get("next_cursor") or self.cursor
        if payload.get("source_truncated") or self.lineage_truncated:
            self.connectivity["ATS"] = "PARTIAL"
        self._merge_events(payload.get("events", []))

    async def refresh_echo(self) -> None:
        summary = await self.sources.echo_summary()
        health = summary.get("health", {})
        self.connectivity["Echo"] = health.get("state", "DISCONNECTED")
        self._merge_events(self.sources.echo_events(summary))
        table = self.query_one("#tool-table", DataTable)
        table.clear()
        telemetry = summary.get("telemetry", {})
        rows = (
            telemetry.get("data", {}).get("rows", [])
            if telemetry.get("state") == "OBSERVED"
            else []
        )
        if not rows:
            table.add_row(
                "Echo MCP telemetry", "—", "—", "—", "—", telemetry.get("state", "UNAVAILABLE")
            )
        for row in rows:
            table.add_row(
                safe_text(row.get("tool_name"), 40),
                str(row.get("calls", "—")),
                f"{row.get('avg_ms', '—')}ms",
                f"{row.get('p95_ms', '—')}ms",
                str(row.get("errs", "—")),
                safe_text(row.get("last_call"), 24),
            )

    async def refresh_system(self) -> None:
        status = await self.sources.local_status()
        rendered = Text()
        for name, result in status.items():
            state = result.get("state", "UNAVAILABLE")
            if rendered:
                rendered.append("\n")
            rendered.append(f"{state:14}", style=STATE_COLORS.get(state, "white"))
            rendered.append(f" {name}\n{safe_text(result.get('summary'), 500)}")
        self.query_one("#system", Static).update(rendered)

    def _merge_events(self, incoming: list[dict]) -> None:
        changed = False
        for event in incoming:
            event_id = event.get("id")
            if event_id and event_id not in self.event_by_id:
                self.event_by_id[event_id] = event
                changed = True
        if changed:
            self.events = sorted(
                self.event_by_id.values(),
                key=lambda row: (row.get("timestamp", ""), row.get("id", "")),
            )[-2000:]
            self.event_by_id = {row["id"]: row for row in self.events}
            self.render_events()

    def filtered_events(self) -> list[dict]:
        rows = self.events
        for key, needle in self.filters.items():
            needle = needle.strip().lower()
            if not needle:
                continue
            if key == "task":
                rows = [
                    row
                    for row in rows
                    if str(row.get("task_id") or "").lower() == needle.lstrip("#")
                ]
            elif key == "session":
                rows = [
                    row
                    for row in rows
                    if needle
                    in json.dumps(
                        {
                            "session": row.get("session_id"),
                            "source": row.get("source"),
                            "target": row.get("target"),
                        }
                    ).lower()
                ]
            elif key == "agent":
                rows = [
                    row
                    for row in rows
                    if needle
                    in json.dumps(
                        {"source": row.get("source"), "target": row.get("target")}
                    ).lower()
                ]
            elif key == "type":
                rows = [row for row in rows if row.get("event_type", "").lower() == needle]
            else:
                rows = [row for row in rows if needle in json.dumps(row).lower()]
        return rows

    def render_events(self) -> None:
        table = self.query_one("#exchange", DataTable)
        table.clear(columns=False)
        for event in self.filtered_events():
            kind = event.get("event_type", "SYSTEM")
            color = EVENT_COLORS.get(kind, "white")
            source = (event.get("source") or {}).get("agent")
            target = (event.get("target") or {}).get("agent")
            try:
                when = (
                    datetime.fromisoformat(event.get("timestamp", ""))
                    .astimezone()
                    .strftime("%H:%M:%S")
                )
            except ValueError:
                when = safe_text(event.get("timestamp"), 12)
            state = event.get("integrity", "UNAVAILABLE")
            direction = Text()
            if source:
                direction.append(str(source), style=self._agent_color(str(source)))
            if source and target:
                direction.append(" → ", style="bright_black")
            if target:
                direction.append(str(target), style=self._agent_color(str(target)))
            if not source and not target:
                direction.append("no explicit direction", style="bright_black")
            table.add_row(
                when,
                Text(kind, style=color),
                direction,
                safe_text(event.get("summary"), 100),
                Text(state, style=STATE_COLORS.get(state, "white")),
                key=event["id"],
            )
        if self.follow and table.row_count:
            table.move_cursor(row=table.row_count - 1)

    def render_connectivity(self) -> None:
        rendered = Text()
        for name, state in self.connectivity.items():
            if rendered:
                rendered.append("  ")
            rendered.append(f"{name} {state}", style=STATE_COLORS.get(state, "white"))
        rendered.append(f"  follow={'PAUSED' if self.paused else 'LIVE'}")
        active = [f"{name}={value}" for name, value in self.filters.items() if value]
        if active:
            rendered.append("  filters " + " ".join(active))
        self.query_one("#connectivity", Static).update(rendered)

    async def on_list_view_selected(self, message: ListView.Selected) -> None:
        session_id = message.item.name or ""
        self.selected_session_id = session_id
        self.filters["session"] = session_id
        row = next((item for item in self.sessions if item.get("id") == session_id), None)
        if row:
            self.selected_task_id = row.get("task_id")
            authority = {
                "session": session_id,
                "agent": row.get("agent"),
                "mode": row.get("effective_mode"),
                "scope": row.get("scope"),
                "task_id": row.get("task_id"),
                "ats_health": row.get("ats_health"),
                "authority": row.get("effective_authority"),
                "parent": row.get("parent_session_id"),
                "children": row.get("child_session_ids"),
            }
            if self.selected_task_id:
                authority["tower"] = await self.refresh_task(self.selected_task_id)
            self.query_one("#task", Static).update(json.dumps(authority, indent=2, default=str))
        self.render_events()
        self.render_connectivity()

    async def refresh_task(self, task_id: int) -> dict:
        task = await self.sources.tower_task(task_id)
        if task.get("state") == "OBSERVED":
            fingerprint = {
                key: task.get(key)
                for key in ("status", "gate", "updated_at", "claim", "blocked_by")
            }
            if self.task_snapshot != fingerprint:
                self.task_snapshot = fingerprint
                timestamp = task.get("updated_at") or datetime.now().astimezone().isoformat()
                self._merge_events(
                    [
                        {
                            "id": f"tower-task:{task_id}:{timestamp}",
                            "timestamp": timestamp,
                            "event_type": "TASK",
                            "integrity": "PARTIAL",
                            "session_id": self.selected_session_id,
                            "task_id": task_id,
                            "source": None,
                            "target": None,
                            "summary": f"Tower task #{task_id} current state",
                            "status": task.get("status"),
                            "source_record": f"tower_tasks:{task_id}",
                            "detail": fingerprint,
                        }
                    ]
                )
        return task

    def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        if message.data_table.id != "exchange" or message.row_key.value is None:
            return
        event = self.event_by_id.get(str(message.row_key.value))
        if event:
            self.query_one("#detail", Static).update(self._event_detail(event, expanded=False))

    def _event_detail(self, event: dict, *, expanded: bool) -> str:
        detail = event.get("detail", {})
        body = {
            "integrity": event.get("integrity"),
            "source_record": event.get("source_record"),
            "source": event.get("source"),
            "target": event.get("target"),
            "session_id": event.get("session_id"),
            "task_id": event.get("task_id"),
            "status": event.get("status"),
            "detail": (
                detail if expanded else {key: value for key, value in list(detail.items())[:5]}
            ),
        }
        return f"{event.get('event_type')}  {event.get('timestamp')}\n{event.get('summary')}\n\n{json.dumps(body, indent=2, default=str)}"

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.render_connectivity()

    def _show_filter(self, kind: str, preset: str = "") -> None:
        self.filter_kind = kind
        widget = self.query_one("#filter", Input)
        widget.placeholder = f"{kind} filter (empty clears)"
        widget.value = preset or self.filters[kind]
        widget.add_class("visible")
        widget.focus()

    def action_search(self) -> None:
        self._show_filter("search")

    def action_task_filter(self) -> None:
        self._show_filter("task")

    def action_agent_filter(self) -> None:
        self._show_filter("agent")

    def action_session_filter(self) -> None:
        self._show_filter("session")

    def action_filter_type(self) -> None:
        kinds = [
            "",
            "ATS",
            "HANDOFF",
            "RULING",
            "BLOCKER",
            "ECHO",
            "TOOL",
            "REVIEW",
            "TASK",
            "SYSTEM",
        ]
        current = self.filters["type"].upper()
        self.filters["type"] = (
            kinds[(kinds.index(current) + 1) % len(kinds)] if current in kinds else ""
        )
        self.render_events()
        self.render_connectivity()

    def action_echo_filter(self) -> None:
        self.filters["type"] = "ECHO"
        self.render_events()
        self.render_connectivity()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        self.filters[self.filter_kind] = message.value.strip()
        message.input.remove_class("visible")
        self.render_events()
        self.render_connectivity()
        self.query_one("#exchange", DataTable).focus()

    async def action_expand(self) -> None:
        table = self.query_one("#exchange", DataTable)
        if table.row_count and table.cursor_row >= 0:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            event = self.event_by_id.get(str(key))
            if event:
                if not str(event.get("id", "")).startswith(("echo-", "tower-task:")):
                    try:
                        payload = await self.sources.ats_event_detail(str(event["id"]))
                        expanded = payload.get("events", [])
                        if expanded:
                            event.update(expanded[0])
                    except Exception:
                        event["detail"]["expansion"] = "UNAVAILABLE"
                if event.get("event_type") == "ECHO" and str(event.get("id", "")).startswith(
                    "echo-preflight:"
                ):
                    request_id = str(event["id"]).split(":", 1)[1]
                    event["detail"]["provenance"] = await self.sources.echo_preflight_provenance(
                        request_id
                    )
                self.query_one("#detail", Static).update(self._event_detail(event, expanded=True))

    @staticmethod
    def _agent_color(agent: str) -> str:
        name = agent.lower()
        if "claude" in name:
            return "magenta"
        if "codex" in name:
            return "cyan"
        if "echo" in name:
            return "green"
        if "ats" in name:
            return "blue"
        return "white"

    def action_down(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_down()
        elif isinstance(focused, ListView):
            focused.action_cursor_down()

    def action_up(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_up()
        elif isinstance(focused, ListView):
            focused.action_cursor_up()

    def action_focus_agents(self) -> None:
        self.query_one("#agent-list", ListView).focus()

    def action_focus_exchange(self) -> None:
        self.query_one("#exchange", DataTable).focus()

    def action_focus_task(self) -> None:
        self.query_one("#task", Static).focus()

    def action_focus_tools(self) -> None:
        self.query_one("#tool-table", DataTable).focus()

    def action_focus_system(self) -> None:
        self.query_one("#system", Static).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 105, "narrow")
