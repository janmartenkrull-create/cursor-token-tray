#!/usr/bin/env python3
"""Taskbar deskband display for Cursor token billing (polls every 5 minutes)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydeskband.pydeskband import ControlPipe, Justification

POLL_SECONDS = 300
API_URL = "https://cursor.com/api/usage-summary"
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
USER_ID_RE = re.compile(r"user_[a-zA-Z0-9]{20,}")

LABEL_COLOR = (170, 178, 188)
ERROR_COLOR = (230, 120, 120)
WARN_COLOR = (230, 170, 90)


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
    if pct >= 90:
        return (255, 120, 120)
    if pct >= 70:
        return (255, 190, 110)
    return (156, 163, 175)


def _value_text(snapshot: UsageSnapshot, side: str) -> str:
    if snapshot.error:
        return "?"
    if snapshot.is_unlimited:
        return "∞"
    if side == "cursor":
        if snapshot.display_mode == "overall" and snapshot.label:
            return snapshot.label
        if snapshot.cursor_pct is None:
            return "?"
        return f"{int(snapshot.cursor_pct)}%"
    if snapshot.display_mode == "overall" and snapshot.label:
        return snapshot.label
    if snapshot.other_pct is None:
        return "?"
    return f"{int(snapshot.other_pct)}%"


def _value_color(snapshot: UsageSnapshot, side: str) -> tuple[int, int, int]:
    if snapshot.error:
        return ERROR_COLOR
    if snapshot.is_unlimited:
        return (150, 220, 170)
    pct = snapshot.cursor_pct if side == "cursor" else snapshot.other_pct
    if pct is None:
        return WARN_COLOR
    return pct_arc_color(pct)


def _error_headline(snapshot: UsageSnapshot) -> str:
    if snapshot.error == "no_session":
        return "Cursor: nicht angemeldet"
    if snapshot.error == "http":
        return "Cursor: API-Fehler"
    if snapshot.error == "network":
        return "Cursor: offline"
    return snapshot.label


def render_deskband(pipe: ControlPipe, snapshot: UsageSnapshot) -> None:
    pipe.clear()

    if snapshot.error:
        pipe.add_new_text_info(_error_headline(snapshot), *ERROR_COLOR)
        pipe.paint()
        return

    if snapshot.display_mode == "overall":
        title = pipe.add_new_text_info("Included", *LABEL_COLOR)
        value = pipe.add_new_text_info(_value_text(snapshot, "cursor"), *_value_color(snapshot, "cursor"))
        value.justify_this_with_respect_to_that(title, Justification.RIGHT_OF, gap=8)
        pipe.paint()
        return

    cursor_title = pipe.add_new_text_info("Cursor", *LABEL_COLOR)
    cursor_value = pipe.add_new_text_info(
        _value_text(snapshot, "cursor"),
        *_value_color(snapshot, "cursor"),
    )
    cursor_value.justify_this_with_respect_to_that(cursor_title, Justification.RIGHT_OF, gap=6)

    other_title = pipe.add_new_text_info("Other", *LABEL_COLOR)
    other_title.justify_this_with_respect_to_that(cursor_value, Justification.RIGHT_OF, gap=18)

    other_value = pipe.add_new_text_info(
        _value_text(snapshot, "other"),
        *_value_color(snapshot, "other"),
    )
    other_value.justify_this_with_respect_to_that(other_title, Justification.RIGHT_OF, gap=6)

    pipe.paint()


def deskband_available() -> bool:
    try:
        with ControlPipe() as pipe:
            pipe.get_height()
        return True
    except FileNotFoundError:
        return False


class DeskbandApp:
    def run(self) -> None:
        while True:
            snapshot = fetch_usage()
            with ControlPipe() as pipe:
                render_deskband(pipe, snapshot)
            time.sleep(POLL_SECONDS)


def main() -> None:
    from install_self import ensure_autostart, maybe_install

    maybe_install()
    ensure_autostart()

    if deskband_available():
        DeskbandApp().run()
        return

    from taskbar_strip import run_taskbar_strip

    run_taskbar_strip()


if __name__ == "__main__":
    main()
