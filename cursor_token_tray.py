#!/usr/bin/env python3
"""Windows system-tray display for Cursor token billing (polls every 5 minutes)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
import pystray

POLL_SECONDS = 300
API_URL = "https://cursor.com/api/usage-summary"
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
USER_ID_RE = re.compile(r"user_[a-zA-Z0-9]{20,}")


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


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("segoeuib.ttf", bold),
        ("segoeui.ttf", not bold),
        ("arialbd.ttf", bold),
        ("arial.ttf", not bold),
    )
    for name, want_bold in candidates:
        if want_bold != bold:
            continue
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


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


def render_icon(snapshot: UsageSnapshot, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(2, int(size * 0.06))
    bbox = (pad, pad, size - pad, size - pad)
    cx = size / 2
    ring = max(2, int(size * 0.11))
    bg = (30, 32, 36, 255)
    track = (54, 58, 64, 255)
    divider = (42, 45, 50, 255)

    draw.ellipse(bbox, fill=bg)

    if snapshot.error:
        font = _load_font(max(8, size // 5), bold=True)
        text = "?"
        tb = draw.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        draw.text(
            (cx - tw / 2, cx - th / 2 - 1),
            text,
            fill=(150, 150, 150, 255),
            font=font,
        )
        return img

    # Left semicircle: top -> left -> bottom (90° .. 270°)
    draw.arc(bbox, 90, 270, fill=track, width=ring)
    # Right semicircle: bottom -> right -> top (270° .. 90°)
    draw.arc(bbox, 270, 450, fill=track, width=ring)

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

    if snapshot.display_mode == "overall":
        accent = pct_arc_color((snapshot.cursor_pct or 0) if snapshot.cursor_pct else 0)
        _draw_arc_progress(draw, bbox, 90, 180, left_ratio, accent, ring)
        _draw_arc_progress(draw, bbox, 270, 180, right_ratio, accent, ring)
    else:
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

    draw.line(
        [(cx, pad + ring // 2), (cx, size - pad - ring // 2)],
        fill=divider,
        width=max(1, size // 32),
    )

    if snapshot.display_mode == "overall":
        center_text = (
            f"{int(snapshot.cursor_pct)}%"
            if snapshot.cursor_pct is not None
            else "…"
        )
        font = _load_font(max(9, size // 4), bold=True)
        color = (143, 163, 184, 255)
    elif snapshot.is_unlimited:
        center_text = "∞"
        font = _load_font(max(10, size // 3), bold=True)
        color = (143, 163, 184, 255)
    else:
        left_n = "?" if snapshot.cursor_pct is None else str(int(snapshot.cursor_pct))
        right_n = "?" if snapshot.other_pct is None else str(int(snapshot.other_pct))
        font = _load_font(max(7, size // 6), bold=True)
        for text, x_frac, accent in (
            (left_n, 0.30, pct_arc_color(snapshot.cursor_pct or 0)),
            (right_n, 0.70, pct_arc_color(snapshot.other_pct or 0)),
        ):
            tb = draw.textbbox((0, 0), text, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            draw.text(
                (size * x_frac - tw / 2, cx - th / 2 - 1),
                text,
                fill=(*accent, 255),
                font=font,
            )
        return img

    tb = draw.textbbox((0, 0), center_text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    draw.text(
        (cx - tw / 2, cx - th / 2 - 1),
        center_text,
        fill=color,
        font=font,
    )
    return img


class TrayApp:
    def __init__(self) -> None:
        self._snapshot = UsageSnapshot("…", "Loading…", None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._icon = pystray.Icon(
            "cursor-token-tray",
            render_icon(self._snapshot),
            "Cursor Token Usage",
            menu=pystray.Menu(
                pystray.MenuItem("Jetzt aktualisieren", self._on_refresh),
                pystray.MenuItem("Beenden", self._on_exit),
            ),
        )

    def _apply_snapshot(self, snapshot: UsageSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot
        self._icon.icon = render_icon(snapshot)
        self._icon.title = snapshot.tooltip.replace("\n", " · ")

    def _poll_once(self) -> None:
        self._apply_snapshot(fetch_usage())

    def _on_refresh(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        threading.Thread(target=self._poll_once, daemon=True).start()

    def _on_exit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._stop.set()
        self._icon.stop()

    def _poll_loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            self._poll_once()

    def run(self) -> None:
        self._poll_once()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._icon.run()


def main() -> None:
    TrayApp().run()


if __name__ == "__main__":
    main()
