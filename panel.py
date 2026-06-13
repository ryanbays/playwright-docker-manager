#!/usr/bin/env python3
"""
panel.py – Multi-host Playwright auth-state manager.

Algorithm (mirrors create-state.js):
  1. Find the Chromium executable via the playwright Python package.
  2. Launch raw Chromium with a fresh --user-data-dir so the user can log in
     manually (Cloudflare, SSO, etc.).  Wait for the process to exit.
  3. Launch a Playwright persistent context (headless) against the same
     profile dir, navigate to the target URL, wait 2 s for session hydration,
     then export storageState → a local JSON file.
  4. Verify the state by launching a plain headless browser that loads the
     state file and navigates to the URL.
  5. POST /upload the JSON to the currently selected host, then clean up the
     local temp files.

Multi-host:
  • Hosts are persisted in hosts.json  { "hosts": [{"name": "...", "url": "..."}] }
  • The sidebar lists all hosts; clicking one makes it active.
  • Add / Remove host via the sidebar controls.
  • All API calls (upload, remove, logs, clear logs) go to the active host.

Layout:
  Left sidebar  – host list + add/remove controls
  Right area    – active-host bar on top, then a TabbedContent whose tabs are:
                    Auth State | Remove State | API Log | Worker Log | Activity
"""

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Log,
    Static,
    TabbedContent,
    TabPane,
)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

HOSTS_FILE = Path("hosts.json")
IS_WINDOWS = sys.platform == "win32"


def load_hosts() -> list[dict]:
    if HOSTS_FILE.exists():
        try:
            data = json.loads(HOSTS_FILE.read_text())
            return data.get("hosts", [])
        except Exception:
            pass
    return []


def save_hosts(hosts: list[dict]) -> None:
    HOSTS_FILE.write_text(json.dumps({"hosts": hosts}, indent=2))


# ---------------------------------------------------------------------------
# Core three-step algorithm  (direct Python rewrite of create-state.js)
# ---------------------------------------------------------------------------

async def create_auth_state(target_url: str, log_fn) -> Path:
    """
    Full three-step flow:
      1. Raw Chromium (manual login) → wait for close
      2. Persistent headless context → export storageState JSON
      3. Verify with plain headless + storageState
    Returns the path to the saved state JSON file.
    """
    profile_dir = Path(tempfile.mkdtemp(prefix="pw-profile-"))
    state_file  = Path(tempfile.mktemp(suffix="-auth-state.json"))

    log_fn(f"[step 1/3] Profile dir: {profile_dir}")

    # ── Step 1: resolve executable then open manual Chromium ────────────────
    async with async_playwright() as p:
        executable = p.chromium.executable_path
        log_fn(f"[step 1/3] Chromium executable: {executable}")

    args = [
        executable,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--start-maximized",
        target_url,
    ]

    log_fn("[step 1/3] Launching Chromium – log in then CLOSE the browser window.")

    proc = subprocess.Popen(
        args,
        shell=IS_WINDOWS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    while proc.poll() is None:          # poll so TUI stays responsive
        await asyncio.sleep(0.5)

    log_fn("[step 1/3] Chromium closed.")

    # ── Step 2: export storageState from persistent context ──────────────────
    log_fn("[step 2/3] Loading persistent profile into Playwright (headless)…")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        page = await context.new_page()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            log_fn(f"[step 2/3] Warning during goto: {exc}")

        await asyncio.sleep(2)          # hydrate session state (mirrors waitForTimeout(2000))

        await context.storage_state(path=str(state_file))
        await context.close()

    log_fn(f"[step 2/3] Storage state saved → {state_file}")

    # ── Step 3: verify with plain headless + state file ──────────────────────
    log_fn("[step 3/3] Verifying headless session with saved state…")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        ctx  = await browser.new_context(storage_state=str(state_file))
        page = await ctx.new_page()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
            log_fn(f"[step 3/3] Headless session verified at {target_url}")
        except Exception as exc:
            log_fn(f"[step 3/3] Warning during verification: {exc}")
        await browser.close()

    shutil.rmtree(profile_dir, ignore_errors=True)
    log_fn("Done – state file ready for upload.")
    return state_file


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class HostAPI:
    """Thin async wrapper around the host's REST API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def status(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(self._url("/status"))
            r.raise_for_status()
            return r.json()

    async def upload(self, file_path: Path) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            with open(file_path, "rb") as fh:
                r = await c.post(
                    self._url("/upload"),
                    files={"file": (file_path.name, fh, "application/json")},
                )
            r.raise_for_status()
            return r.json()

    async def remove(self, file_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self._url(f"/remove/{file_id}"))
            r.raise_for_status()
            return r.json()

    async def states(self) -> list[str]:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(self._url("/states"))
            r.raise_for_status()
            return r.json().get("states", [])

    async def get_log(self, kind: str) -> str:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(self._url(f"/logs/{kind}"))
            return r.text if r.status_code == 200 else f"[error {r.status_code}] {r.text}"

    async def clear_log(self, kind: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.delete(self._url(f"/logs/{kind}"))
            r.raise_for_status()
            return r.json()


# ---------------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------------

class HostItem(ListItem):
    def __init__(self, name: str, url: str, index: int):
        super().__init__(Label(f"{name}\n{url}"))
        self.host_name  = name
        self.host_url   = url
        self.host_index = index


class StateItem(ListItem):
    def __init__(self, state_id: str):
        super().__init__(Label(state_id))
        self.state_id = state_id


# ---------------------------------------------------------------------------
# Main TUI application
# ---------------------------------------------------------------------------

class BeansPanel(App):

    CSS = """
    /* ── Root layout ─────────────────────────────────────────── */
    Screen {
        layout: horizontal;
    }

    /* ── Sidebar ─────────────────────────────────────────────── */
    #sidebar {
        width: 30;
        border-right: solid $primary-darken-2;
        layout: vertical;
        padding: 0 1;
    }

    #sidebar-title {
        text-style: bold;
        color: $primary;
        padding: 1 0 0 0;
        height: 2;
    }

    #host-list {
        height: 1fr;
        border: solid $primary-darken-3;
        margin-top: 1;
    }

    #sidebar-controls {
        height: auto;
        margin-top: 1;
        layout: vertical;
    }

    #new-host-name {
        margin-bottom: 0;
    }

    #new-host-url {
        margin-bottom: 1;
    }

    #btn-add-host {
        width: 1fr;
        margin-bottom: 1;
    }

    #btn-remove-host {
        width: 1fr;
        margin-bottom: 1;
    }

    /* ── Main area ───────────────────────────────────────────── */
    #main {
        width: 1fr;
        layout: vertical;
    }

    /* ── Active-host bar ─────────────────────────────────────── */
    #active-host-bar {
        height: 3;
        background: $primary-darken-3;
        padding: 0 2;
        layout: horizontal;
        align: left middle;
        margin-bottom: 1;
    }

    #active-host-label {
        width: 1fr;
        color: $text;
        text-style: bold;
    }

    #status-dot {
        width: 16;
        color: $success;
        text-align: right;
    }

    /* ── Tabs fill remaining height ──────────────────────────── */
    TabbedContent {
        height: 1fr;
    }

    TabPane {
        padding: 0;
    }

    /* ── Auth-state tab ──────────────────────────────────────── */
    #pane-auth {
        layout: vertical;
        padding: 1 2;
    }

    #auth-url-row {
        height: 3;
        layout: horizontal;
        margin-bottom: 1;
    }

    #target-url {
        width: 1fr;
        margin-right: 1;
    }

    #btn-create-state {
        width: 26;
    }

    #btn-check-status {
        width: 18;
        margin-left: 1;
    }

    #auth-activity-title {
        height: 2;
        text-style: bold;
        color: $accent;
        padding: 1 0 0 0;
    }

    #auth-activity-log {
        height: 1fr;
        border: solid $primary-darken-3;
    }

    /* ── Remove-state tab ────────────────────────────────────── */
    #pane-remove {
        layout: vertical;
        padding: 1 2;
    }

    #remove-row {
        height: 3;
        layout: horizontal;
        margin-bottom: 1;
    }

    #remove-id-input {
        width: 1fr;
        margin-right: 1;
    }

    #btn-remove-state {
        width: 22;
    }

    #btn-refresh-states {
        width: 22;
        margin-right: 1;
    }

    #remove-hint {
        color: $text-muted;
        padding: 0 0 1 0;
    }

    #states-title {
        height: 2;
        text-style: bold;
        color: $accent;
        padding: 1 0 0 0;
    }

    #states-list {
        height: 1fr;
        border: solid $primary-darken-3;
    }

    /* ── Shared log-tab styles ───────────────────────────────── */
    .log-pane {
        layout: vertical;
        height: 1fr;
    }

    .log-toolbar {
        height: 4;
        layout: horizontal;
        padding: 0 5;
        align: left middle;
        border-bottom: solid $primary-darken-3;
    }

    .btn-refresh {
        width: 16;
        margin-right: 1;
    }

    .btn-clear {
        width: 16;
    }

    .remote-log {
        height: 1fr;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_logs", "Refresh logs"),
    ]

    active_host_index: reactive[int] = reactive(-1)

    def __init__(self):
        super().__init__()
        self._hosts: list[dict] = load_hosts()

    # ── Composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal():

            # ── Left sidebar ─────────────────────────────────────────────────
            with Vertical(id="sidebar"):
                yield Static("🖥  Hosts", id="sidebar-title")
                yield ListView(id="host-list")
                with Vertical(id="sidebar-controls"):
                    yield Input(placeholder="Name",              id="new-host-name")
                    yield Input(placeholder="http://host:port",  id="new-host-url")
                    yield Button("＋ Add Host",    id="btn-add-host",    variant="success")
                    yield Button("✕ Remove Host",  id="btn-remove-host", variant="error")

            # ── Right main area ───────────────────────────────────────────────
            with Vertical(id="main"):

                # Active-host bar always visible at the top
                with Horizontal(id="active-host-bar"):
                    yield Static("No host selected", id="active-host-label")
                    yield Static("● offline",         id="status-dot")

                # All child panels live as tabs
                with TabbedContent(id="main-tabs"):

                    # ── Tab 1: Auth State ─────────────────────────────────────
                    with TabPane("🔑 Auth State", id="tab-auth"):
                        with Vertical(id="pane-auth"):
                            with Horizontal(id="auth-url-row"):
                                yield Input(
                                    placeholder="https://example.com  (target URL for auth)",
                                    id="target-url",
                                )
                                yield Button(
                                    "🔑 Create & Upload",
                                    id="btn-create-state",
                                    variant="success",
                                )
                                yield Button(
                                    "⚡ Status",
                                    id="btn-check-status",
                                    variant="primary",
                                )
                            yield Static("📋 Activity", id="auth-activity-title")
                            yield Log(id="activity-log", highlight=True)

                    # ── Tab 2: Remove State ───────────────────────────────────
                    with TabPane("🗑  Remove State", id="tab-remove"):
                        with Vertical(id="pane-remove"):
                            yield Static(
                                "Enter the UUID returned by /upload — or pick one from the list below — "
                                "to delete that auth state from the host.",
                                id="remove-hint",
                            )
                            with Horizontal(id="remove-row"):
                                yield Input(
                                    placeholder="Auth state UUID",
                                    id="remove-id-input",
                                )
                                yield Button(
                                    "🗑  Remove State",
                                    id="btn-remove-state",
                                    variant="warning",
                                )
                            yield Static("🗂  States on host", id="states-title")
                            yield Button(
                                "↺ Refresh States",
                                id="btn-refresh-states",
                                variant="primary",
                            )
                            yield ListView(id="states-list")

                    # ── Tab 3: API Log ────────────────────────────────────────
                    with TabPane("📄 API Log", id="tab-api"):
                        with Vertical(classes="log-pane"):
                            with Horizontal(classes="log-toolbar"):
                                yield Button("↺ Refresh", id="btn-refresh-api",  classes="btn-refresh")
                                yield Button("✕ Clear",   id="btn-clear-api",    classes="btn-clear", variant="warning")
                            yield Log(id="log-api", highlight=True, classes="remote-log")

                    # ── Tab 4: Worker Log ─────────────────────────────────────
                    with TabPane("⚙  Worker Log", id="tab-worker"):
                        with Vertical(classes="log-pane"):
                            with Horizontal(classes="log-toolbar"):
                                yield Button("↺ Refresh", id="btn-refresh-worker", classes="btn-refresh")
                                yield Button("✕ Clear",   id="btn-clear-worker",   classes="btn-clear", variant="warning")
                            yield Log(id="log-worker", highlight=True, classes="remote-log")

        yield Footer()

    def on_mount(self) -> None:
        self._rebuild_host_list()
        if self._hosts:
            self._select_host(0)

    # ── Host list helpers ─────────────────────────────────────────────────────

    def _rebuild_host_list(self) -> None:
        lv = self.query_one("#host-list", ListView)
        lv.clear()
        for i, h in enumerate(self._hosts):
            lv.append(HostItem(h["name"], h["url"], i))

    def _select_host(self, index: int) -> None:
        if not self._hosts or index < 0 or index >= len(self._hosts):
            self.active_host_index = -1
            return
        self.active_host_index = index
        host = self._hosts[index]
        self.query_one("#active-host-label", Static).update(
            f"🖥  {host['name']}  —  {host['url']}"
        )
        self.query_one("#status-dot", Static).update("● …")
        self._activity(f"Switched to host: {host['name']} ({host['url']})")
        self.check_host_status()
        self.fetch_states()

    def _active_api(self) -> HostAPI | None:
        if self.active_host_index < 0:
            return None
        return HostAPI(self._hosts[self.active_host_index]["url"])

    # ── Activity log ─────────────────────────────────────────────────────────

    def _activity(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.query_one("#activity-log", Log).write_line(f"[{ts}] {msg}")

    # ── Events ───────────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, HostItem):
            self._select_host(event.item.host_index)
        elif isinstance(event.item, StateItem):
            self.query_one("#remove-id-input", Input).value = event.item.state_id

    def on_button_pressed(self, event: Button.Pressed) -> None:  # noqa: PLR0912
        bid = event.button.id

        # Sidebar
        if   bid == "btn-add-host":       self._add_host()
        elif bid == "btn-remove-host":    self._remove_selected_host()

        # Auth State tab
        elif bid == "btn-create-state":   self._start_create_state()
        elif bid == "btn-check-status":   self.check_host_status()

        # Remove State tab
        elif bid == "btn-remove-state":   self._remove_state()
        elif bid == "btn-refresh-states": self.fetch_states()

        # API Log tab
        elif bid == "btn-refresh-api":    self.fetch_log("api")
        elif bid == "btn-clear-api":      self.clear_log("api")

        # Worker Log tab
        elif bid == "btn-refresh-worker": self.fetch_log("worker")
        elif bid == "btn-clear-worker":   self.clear_log("worker")

    # ── Host management ───────────────────────────────────────────────────────

    def _add_host(self) -> None:
        name = self.query_one("#new-host-name", Input).value.strip()
        url  = self.query_one("#new-host-url",  Input).value.strip()
        if not name or not url:
            self._activity("Error: provide both name and URL to add a host.")
            return
        self._hosts.append({"name": name, "url": url})
        save_hosts(self._hosts)
        self._rebuild_host_list()
        self.query_one("#new-host-name", Input).clear()
        self.query_one("#new-host-url",  Input).clear()
        self._activity(f"Host added: {name} ({url})")
        self._select_host(len(self._hosts) - 1)

    def _remove_selected_host(self) -> None:
        lv = self.query_one("#host-list", ListView)
        if lv.index is None:
            self._activity("Select a host first.")
            return
        item = lv.children[lv.index]
        if not isinstance(item, HostItem):
            return
        removed = self._hosts.pop(item.host_index)
        save_hosts(self._hosts)
        self._rebuild_host_list()
        self._activity(f"Host removed: {removed['name']}")
        if self._hosts:
            self._select_host(min(item.host_index, len(self._hosts) - 1))
        else:
            self.active_host_index = -1
            self.query_one("#active-host-label", Static).update("No host selected")
            self.query_one("#status-dot", Static).update("● offline")

    # ── Status check ─────────────────────────────────────────────────────────

    @work(exclusive=False, thread=False)
    async def check_host_status(self) -> None:
        api = self._active_api()
        if api is None:
            self._activity("No host selected.")
            return
        dot = self.query_one("#status-dot", Static)
        dot.update("● checking…")
        try:
            data = await api.status()
            if data.get("status") == "ok":
                dot.update("● online")
                self._activity(f"Status OK – {api.base_url}")
            else:
                dot.update("● unknown")
                self._activity(f"Unexpected status response: {data}")
        except Exception as exc:
            dot.update("● offline")
            self._activity(f"Status check failed: {exc}")

    # ── Create & upload state ─────────────────────────────────────────────────

    def _start_create_state(self) -> None:
        api = self._active_api()
        if api is None:
            self._activity("No host selected – cannot create state.")
            return
        url = self.query_one("#target-url", Input).value.strip()
        if not url:
            self._activity("Enter a target URL first.")
            return
        self.query_one("#btn-create-state", Button).disabled = True
        self._create_and_upload_state(url, api)

    @work(exclusive=False, thread=False)
    async def _create_and_upload_state(self, target_url: str, api: HostAPI) -> None:
        btn = self.query_one("#btn-create-state", Button)
        state_file: Path | None = None
        try:
            self._activity(f"Starting auth-state creation for {target_url}")
            state_file = await create_auth_state(target_url, self._activity)
            self._activity("Uploading state file to host…")
            result = await api.upload(state_file)
            self._activity(
                f"✅ Uploaded – ID: {result.get('id', '?')}  file: {result.get('file', '?')}"
            )
        except Exception as exc:
            self._activity(f"❌ Error: {exc}")
        finally:
            if state_file and state_file.exists():
                state_file.unlink(missing_ok=True)
            btn.disabled = False

    # ── Remove state ──────────────────────────────────────────────────────────

    @work(exclusive=False, thread=False)
    async def _remove_state(self) -> None:
        api = self._active_api()
        if api is None:
            self._activity("No host selected.")
            return
        file_id = self.query_one("#remove-id-input", Input).value.strip()
        if not file_id:
            self._activity("Enter the UUID of the state to remove.")
            return
        try:
            result = await api.remove(file_id)
            self._activity(f"Removed state: {result.get('id', file_id)}")
            self.query_one("#remove-id-input", Input).clear()
            self.fetch_states()
        except Exception as exc:
            self._activity(f"❌ Remove failed: {exc}")

    @work(exclusive=False, thread=False)
    async def fetch_states(self) -> None:
        api = self._active_api()
        if api is None:
            self._activity("No host selected.")
            return
        self._activity("Fetching states from host…")
        try:
            states = await api.states()
            lv = self.query_one("#states-list", ListView)
            lv.clear()
            for state_id in states:
                lv.append(StateItem(state_id))
            self._activity(f"States refreshed ({len(states)} on host).")
        except Exception as exc:
            self._activity(f"❌ Failed to fetch states: {exc}")

    # ── Log actions ───────────────────────────────────────────────────────────

    @work(exclusive=False, thread=False)
    async def fetch_log(self, kind: str) -> None:
        api = self._active_api()
        if api is None:
            self._activity("No host selected.")
            return
        self._activity(f"Fetching {kind} log…")
        try:
            text = await api.get_log(kind)
            log_widget = self.query_one(f"#log-{kind}", Log)
            log_widget.clear()
            for line in text.splitlines():
                log_widget.write_line(line)
            self._activity(f"{kind} log refreshed ({len(text.splitlines())} lines).")
        except Exception as exc:
            self._activity(f"❌ Failed to fetch {kind} log: {exc}")

    @work(exclusive=False, thread=False)
    async def clear_log(self, kind: str) -> None:
        api = self._active_api()
        if api is None:
            self._activity("No host selected.")
            return
        try:
            await api.clear_log(kind)
            self.query_one(f"#log-{kind}", Log).clear()
            self._activity(f"{kind} log cleared on host.")
        except Exception as exc:
            self._activity(f"❌ Failed to clear {kind} log: {exc}")

    # ── Keybinding actions ────────────────────────────────────────────────────

    def action_refresh_logs(self) -> None:
        self.fetch_log("api")
        self.fetch_log("worker")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    BeansPanel().run()