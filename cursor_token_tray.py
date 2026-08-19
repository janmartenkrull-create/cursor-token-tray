#!/usr/bin/env python3
"""Windows system-tray display for Cursor token billing (polls every 5 minutes)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
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
TRAY_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


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


def pct_arc_color(pct: float) -> tuple[int, int, int]:
    ratio = pct / 100.0
    if ratio >= 1.0:
        return (214, 96, 96)
    if ratio >= 0.8:
        return (214, 156, 96)
    return (143, 163, 184)


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _badge_text(snapshot: UsageSnapshot) -> tuple[str, str]:
    if snapshot.error:
        return "?", "?"
    if snapshot.is_unlimited:
        return "∞", "∞"
    if snapshot.display_mode == "overall":
        text = (
            f"{int(snapshot.cursor_pct)}"
            if snapshot.cursor_pct is not None
            else "…"
        )
        return text, text
    left = (
        f"{int(snapshot.cursor_pct)}"
        if snapshot.cursor_pct is not None
        else "?"
    )
    right = (
        f"{int(snapshot.other_pct)}"
        if snapshot.other_pct is not None
        else "?"
    )
    return left, right


def render_tray_icon(snapshot: UsageSnapshot, size: int = 256) -> Image.Image:
    """Split-badge tray icon: full square used for two large pool values."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    mid = size // 2
    radius = max(2, size // 16)
    bg = (24, 26, 30, 255)

    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=bg)

    left_text, right_text = _badge_text(snapshot)
    left_color = pct_arc_color(snapshot.cursor_pct or 0)
    right_color = pct_arc_color(snapshot.other_pct or 0)

    if not snapshot.error:
        left_fill = (*left_color, 70)
        right_fill = (*right_color, 70)
        draw.rectangle((1, 1, mid - 1, size - 2), fill=left_fill)
        draw.rectangle((mid + 1, 1, size - 2, size - 2), fill=right_fill)

    draw.line([(mid, 4), (mid, size - 5)], fill=(255, 255, 255, 90), width=max(1, size // 64))

    font_px = max(7, int(size * 0.34))
    font = _load_font(font_px, bold=True)
    stroke = max(1, size // 48)

    for text, cx, color in (
        (left_text, mid * 0.5, left_color),
        (right_text, mid + mid * 0.5, right_color),
    ):
        draw.text(
            (cx, size / 2),
            text,
            fill=(255, 255, 255, 255),
            font=font,
            anchor="mm",
            stroke_width=stroke,
            stroke_fill=(*color, 255),
        )

    return img


def render_tray_icon_set(snapshot: UsageSnapshot) -> Image.Image:
    layers = [render_tray_icon(snapshot, size) for size in TRAY_ICON_SIZES]
    master = layers[-1]
    master.save = _make_ico_save(master, layers)  # type: ignore[method-assign]
    return master


def _make_ico_save(master: Image.Image, layers: list[Image.Image]):
    def save(fp, format=None, **kwargs):
        if format and format.upper() != "ICO":
            return Image.Image.save(master, fp, format, **kwargs)
        sizes = [(img.width, img.height) for img in layers]
        layers[-1].save(
            fp,
            format="ICO",
            sizes=sizes,
            append_images=layers[:-1],
        )

    return save


class TrayApp:
    def __init__(self) -> None:
        self._snapshot = UsageSnapshot("…", "Loading…", None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._icon = pystray.Icon(
            "cursor-token-tray",
            render_tray_icon_set(self._snapshot),
            "Cursor Token Usage",
            menu=pystray.Menu(
                pystray.MenuItem("Jetzt aktualisieren", self._on_refresh),
                pystray.MenuItem("Beenden", self._on_exit),
            ),
        )

    def _apply_snapshot(self, snapshot: UsageSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot
        self._icon.icon = render_tray_icon_set(snapshot)
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
