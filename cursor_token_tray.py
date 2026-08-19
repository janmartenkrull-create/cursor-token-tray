#!/usr/bin/env python3
"""Windows system-tray display for Cursor token billing (polls every 5 minutes)."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
import threading
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from typing import Any, Callable

from PIL import Image, ImageDraw
import pystray

POLL_SECONDS = 300
API_URL = "https://cursor.com/api/usage-summary"
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
USER_ID_RE = re.compile(r"user_[a-zA-Z0-9]{20,}")
OVERLAY_WIDTH = 220
OVERLAY_HEIGHT = 46
OVERLAY_FONT = 15


@dataclass
class UsageSnapshot:
    label: str
    tooltip: str
    max_ratio: float | None
    cursor_pct: float | None = None
    other_pct: float | None = None
    is_unlimited: bool = False
    display_mode: str = "pools"
    error: str | None = None


def cursor_appdata() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Cursor"


def overlay_config_path() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "cursor-token-tray" / "overlay.json"


def db_path() -> Path:
    return cursor_appdata() / "User" / "globalStorage" / "state.vscdb"


def read_db_value(key: str) -> str | None:
    path = db_path()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def extract_user_id(raw: str | None) -> str | None:
    if not raw:
        return None
    if "|" in raw:
        for part in raw.split("|"):
            if part.startswith("user_"):
                return part
    return raw if raw.startswith("user_") else None


def find_user_id_in_json_file(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = USER_ID_RE.search(text)
        return match.group(0) if match else None

    scope_user = data.get("scope", {}).get("user", {}).get("id")
    uid = extract_user_id(scope_user) if scope_user else None
    if uid:
        return uid
    return extract_user_id(data.get("did"))


def get_user_id() -> str | None:
    base = cursor_appdata()
    for rel in (
        "sentry/scope_v3.json",
        "sentry/session.json",
        "User/globalStorage/storage.json",
    ):
        uid = find_user_id_in_json_file(base / rel)
        if uid:
            return uid
    return extract_user_id(read_db_value("cursorAuth/cachedSignUpId"))


def get_session_cookie() -> str | None:
    user_id = get_user_id()
    access_token = read_db_value("cursorAuth/accessToken")
    if not user_id or not access_token:
        return None
    return f"{user_id}%3A%3A{access_token}"


def parse_percent(value: Any) -> float | None:
    if isinstance(value, (int, float)) and float(value) == value:
        return max(0.0, min(100.0, float(value)))
    return None


def parse_percent_from_message(msg: Any) -> float | None:
    if not isinstance(msg, str):
        return None
    match = PCT_RE.search(msg)
    if not match:
        return None
    try:
        return max(0.0, min(100.0, float(match.group(1))))
    except ValueError:
        return None


def parse_cents(value: Any) -> int | None:
    if isinstance(value, (int, float)) and float(value) == value:
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def format_cents(cents: int) -> str:
    return f"${cents / 100:.2f}"


def parse_summary(data: dict[str, Any]) -> UsageSnapshot:
    individual = data.get("individualUsage") or {}
    plan = individual.get("plan") or {}
    overall = individual.get("overall") or {}
    on_demand = individual.get("onDemand") or {}
    team = data.get("teamUsage") or {}
    team_on_demand = team.get("onDemand") or {}

    plan_auto = parse_percent(plan.get("autoPercentUsed"))
    plan_api = parse_percent(plan.get("apiPercentUsed"))
    plan_total = parse_percent(plan.get("totalPercentUsed"))
    has_plan_percents = plan_auto is not None or plan_api is not None

    overall_used = parse_cents(overall.get("used"))
    overall_limit = parse_cents(overall.get("limit"))
    has_overall = (
        overall_used is not None
        and overall_limit is not None
        and overall_limit > 0
    )

    cursor_pct = plan_auto
    other_pct = plan_api
    if not has_overall:
        if cursor_pct is None:
            cursor_pct = parse_percent_from_message(
                data.get("autoModelSelectedDisplayMessage")
            )
        if other_pct is None:
            other_pct = parse_percent_from_message(
                data.get("namedModelSelectedDisplayMessage")
            )

    display_mode = "pools" if (has_plan_percents or not has_overall) else "overall"
    membership = str(data.get("membershipType") or "")
    team_like = bool(re.search(r"enterprise|team", membership, re.I))

    lines = [f"Cursor Token Usage · {membership or 'unknown'}"]

    if display_mode == "overall" and overall_used is not None and overall_limit is not None:
        label = f"{format_cents(overall_used)}/{format_cents(overall_limit)}"
        ratio = overall_used / overall_limit if overall_limit else None
        overall_pct = ratio * 100 if ratio is not None else None
        lines.append(f"Included: {label}")
        return UsageSnapshot(
            label=label,
            tooltip="\n".join(lines),
            max_ratio=ratio,
            cursor_pct=overall_pct,
            other_pct=overall_pct,
            is_unlimited=False,
            display_mode="overall",
        )

    c_val = None if data.get("isUnlimited") else cursor_pct
    o_val = None if data.get("isUnlimited") else other_pct
    c = "∞" if data.get("isUnlimited") else (
        f"{int(cursor_pct)}%" if cursor_pct is not None else "?"
    )
    o = "∞" if data.get("isUnlimited") else (
        f"{int(other_pct)}%" if other_pct is not None else "?"
    )
    label = f"C{c if c == '∞' else c.replace('%', '')} O{o if o == '∞' else o.replace('%', '')}"
    if plan_total is not None and not data.get("isUnlimited"):
        ratio = plan_total / 100.0
    else:
        ratios = [p / 100.0 for p in (cursor_pct, other_pct) if p is not None]
        ratio = max(ratios) if ratios else None
    lines.append(f"Cursor Models: {c}")
    lines.append(f"Other Models: {o}")

    od_used = parse_cents(on_demand.get("used")) or parse_cents(
        on_demand.get("usedCents")
    )
    if od_used is None:
        od_used = parse_cents(team_on_demand.get("used")) or parse_cents(
            team_on_demand.get("usedCents")
        )
    if od_used is not None and (team_like or od_used > 0 or on_demand.get("enabled")):
        lines.append(f"On-Demand: {format_cents(od_used)}")

    end = data.get("billingCycleEnd")
    if isinstance(end, str) and end:
        lines.append(f"Cycle ends: {end[:10]}")

    return UsageSnapshot(
        label=label,
        tooltip="\n".join(lines),
        max_ratio=ratio,
        cursor_pct=c_val,
        other_pct=o_val,
        is_unlimited=bool(data.get("isUnlimited")),
        display_mode=display_mode,
    )


def fetch_usage() -> UsageSnapshot:
    cookie = get_session_cookie()
    if not cookie:
        return UsageSnapshot(
            label="No login",
            tooltip="Cursor session not found.\nSign in to Cursor on this PC.",
            max_ratio=None,
            error="no_session",
        )

    req = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Cookie": f"WorkosCursorSessionToken={cookie}",
            "Origin": "https://cursor.com",
            "Referer": "https://cursor.com/dashboard/usage",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return UsageSnapshot(
            label="HTTP err",
            tooltip=f"API error: HTTP {exc.code}",
            max_ratio=None,
            error="http",
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return UsageSnapshot(
            label="Offline",
            tooltip=f"Could not fetch usage:\n{exc}",
            max_ratio=None,
            error="network",
        )

    return parse_summary(data)


def pct_arc_color(pct: float) -> tuple[int, int, int]:
    ratio = pct / 100.0
    if ratio >= 1.0:
        return (214, 96, 96)
    if ratio >= 0.8:
        return (214, 156, 96)
    return (143, 163, 184)


def rgb_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def work_area() -> tuple[int, int, int, int]:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


def load_overlay_position() -> tuple[int, int] | None:
    path = overlay_config_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["x"]), int(data["y"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def save_overlay_position(x: int, y: int) -> None:
    path = overlay_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")


def default_overlay_position() -> tuple[int, int]:
    left, top, right, bottom = work_area()
    x = right - OVERLAY_WIDTH - 180
    y = bottom - OVERLAY_HEIGHT - 6
    return max(left + 8, x), max(top + 8, y)


def _draw_arc_progress(
    draw: ImageDraw.ImageDraw,
    bbox: tuple[float, float, float, float],
    start: float,
    sweep: float,
    ratio: float,
    color: tuple[int, int, int],
    width: int,
) -> None:
    if ratio <= 0:
        return
    end = start + sweep * min(ratio, 1.0)
    draw.arc(bbox, start=start, end=end, fill=(*color, 255), width=width)


def render_tray_icon(snapshot: UsageSnapshot, size: int = 64) -> Image.Image:
    """Small systray glyph — readable numbers live in the taskbar overlay."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 6
    bbox = (pad, pad, size - pad, size - pad)
    ring = 5
    track = (58, 62, 70, 230)
    draw.arc(bbox, 90, 270, fill=track, width=ring)
    draw.arc(bbox, 270, 450, fill=track, width=ring)
    if snapshot.error:
        return img
    left_ratio = (
        1.0
        if snapshot.is_unlimited
        else (snapshot.cursor_pct or 0) / 100.0
        if snapshot.cursor_pct is not None
        else 0.0
    )
    right_ratio = (
        1.0
        if snapshot.is_unlimited
        else (snapshot.other_pct or 0) / 100.0
        if snapshot.other_pct is not None
        else 0.0
    )
    _draw_arc_progress(
        draw,
        bbox,
        90,
        180,
        left_ratio,
        pct_arc_color(snapshot.cursor_pct or 0),
        ring,
    )
    _draw_arc_progress(
        draw,
        bbox,
        270,
        180,
        right_ratio,
        pct_arc_color(snapshot.other_pct or 0),
        ring,
    )
    return img


class TaskbarOverlay:
    def __init__(
        self,
        root: tk.Tk,
        on_refresh: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.root = root
        self.on_refresh = on_refresh
        self.on_exit = on_exit
        self._drag: tuple[int, int] | None = None

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#1a1c20")
        root.wm_attributes("-transparentcolor", "")  # no-op fallback

        pos = load_overlay_position() or default_overlay_position()
        root.geometry(f"{OVERLAY_WIDTH}x{OVERLAY_HEIGHT}+{pos[0]}+{pos[1]}")

        self.frame = tk.Frame(
            root,
            bg="#1a1c20",
            highlightthickness=1,
            highlightbackground="#3a3f48",
        )
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        self.left_var = tk.StringVar(value="…")
        self.right_var = tk.StringVar(value="…")
        self.label_font = tkfont.Font(family="Segoe UI", size=OVERLAY_FONT, weight="bold")
        self.sub_font = tkfont.Font(family="Segoe UI", size=8)

        self.left_label = tk.Label(
            self.frame,
            textvariable=self.left_var,
            font=self.label_font,
            fg="#9fb0c3",
            bg="#1a1c20",
            width=5,
            anchor="e",
        )
        self.left_label.pack(side="left", padx=(10, 4))

        self.canvas = tk.Canvas(
            self.frame,
            width=34,
            height=34,
            bg="#1a1c20",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(side="left", padx=2, pady=4)

        self.right_label = tk.Label(
            self.frame,
            textvariable=self.right_var,
            font=self.label_font,
            fg="#d49a63",
            bg="#1a1c20",
            width=5,
            anchor="w",
        )
        self.right_label.pack(side="left", padx=(4, 10))

        for widget in (self.frame, self.left_label, self.canvas, self.right_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)
            widget.bind("<Button-3>", self._show_menu)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Jetzt aktualisieren", command=on_refresh)
        self.menu.add_command(label="Beenden", command=on_exit)

        self.update(UsageSnapshot("…", "Loading…", None))

    def _start_drag(self, event: tk.Event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event: tk.Event) -> None:
        self._drag = None
        save_overlay_position(self.root.winfo_x(), self.root.winfo_y())

    def _show_menu(self, event: tk.Event) -> None:
        self.menu.tk_popup(event.x_root, event.y_root)

    def _draw_canvas(
        self,
        left_pct: float | None,
        right_pct: float | None,
        is_unlimited: bool,
    ) -> None:
        self.canvas.delete("all")
        cx, cy, r = 17, 17, 14
        track = "#3a3f48"
        width = 4
        self.canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=90, extent=180, style="arc", outline=track, width=width,
        )
        self.canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=270, extent=180, style="arc", outline=track, width=width,
        )
        if is_unlimited:
            color = rgb_hex((143, 163, 184))
            self.canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=90, extent=180, style="arc", outline=color, width=width,
            )
            self.canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=270, extent=180, style="arc", outline=color, width=width,
            )
            return
        if left_pct is not None and left_pct > 0:
            self.canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=90,
                extent=max(2, 180 * min(left_pct, 100) / 100),
                style="arc",
                outline=rgb_hex(pct_arc_color(left_pct)),
                width=width,
            )
        if right_pct is not None and right_pct > 0:
            self.canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=270,
                extent=max(2, 180 * min(right_pct, 100) / 100),
                style="arc",
                outline=rgb_hex(pct_arc_color(right_pct)),
                width=width,
            )

    def update(self, snapshot: UsageSnapshot) -> None:
        if snapshot.error:
            self.left_var.set("?")
            self.right_var.set("?")
            self.left_label.configure(fg="#888888")
            self.right_label.configure(fg="#888888")
            self._draw_canvas(None, None, False)
            self.root.title(snapshot.tooltip)
            return

        if snapshot.is_unlimited:
            self.left_var.set("∞")
            self.right_var.set("∞")
        elif snapshot.display_mode == "overall":
            text = (
                f"{int(snapshot.cursor_pct)}%"
                if snapshot.cursor_pct is not None
                else "…"
            )
            self.left_var.set(text)
            self.right_var.set(text)
        else:
            self.left_var.set(
                f"{int(snapshot.cursor_pct)}%"
                if snapshot.cursor_pct is not None
                else "?"
            )
            self.right_var.set(
                f"{int(snapshot.other_pct)}%"
                if snapshot.other_pct is not None
                else "?"
            )

        self.left_label.configure(fg=rgb_hex(pct_arc_color(snapshot.cursor_pct or 0)))
        self.right_label.configure(fg=rgb_hex(pct_arc_color(snapshot.other_pct or 0)))
        self._draw_canvas(snapshot.cursor_pct, snapshot.other_pct, snapshot.is_unlimited)
        self.root.title(snapshot.tooltip.replace("\n", " · "))


class TrayApp:
    def __init__(self, overlay: TaskbarOverlay, root: tk.Tk) -> None:
        self._overlay = overlay
        self._root = root
        self._snapshot = UsageSnapshot("…", "Loading…", None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._icon = pystray.Icon(
            "cursor-token-tray",
            render_tray_icon(self._snapshot),
            "Cursor Token Usage",
            menu=pystray.Menu(
                pystray.MenuItem("Jetzt aktualisieren", self._menu_refresh),
                pystray.MenuItem("Beenden", self._menu_exit),
            ),
        )

    def _apply_snapshot(self, snapshot: UsageSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

        def apply_ui() -> None:
            self._overlay.update(snapshot)
            self._icon.icon = render_tray_icon(snapshot)
            self._icon.title = snapshot.tooltip.replace("\n", " · ")

        self._root.after(0, apply_ui)

    def _poll_once(self) -> None:
        self._apply_snapshot(fetch_usage())

    def _menu_refresh(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        threading.Thread(target=self._poll_once, daemon=True).start()

    def _menu_exit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        self._icon.stop()
        self._root.after(0, self._root.destroy)

    def _poll_loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            self._poll_once()

    def run_tray(self) -> None:
        self._poll_once()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._icon.run()


def main() -> None:
    root = tk.Tk()
    app_holder: dict[str, TrayApp] = {}

    def on_exit() -> None:
        app = app_holder.get("app")
        if app:
            app.stop()

    def on_refresh() -> None:
        app = app_holder.get("app")
        if app:
            threading.Thread(target=app._poll_once, daemon=True).start()

    overlay = TaskbarOverlay(root, on_refresh=on_refresh, on_exit=on_exit)
    app = TrayApp(overlay, root)
    app_holder["app"] = app
    threading.Thread(target=app.run_tray, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    main()
