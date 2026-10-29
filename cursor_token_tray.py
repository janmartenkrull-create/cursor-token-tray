#!/usr/bin/env python3
"""Taskbar deskband display for Cursor token billing (polls every 5 minutes)."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from ctypes import (
    POINTER,
    byref,
    c_char,
    c_ubyte,
    c_ulong,
    c_ulonglong,
    c_void_p,
    cast,
    sizeof,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydeskband.pydeskband import ControlPipe, Justification

POLL_SECONDS = 300
API_URL = "https://cursor.com/api/usage-summary"
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
USER_ID_RE = re.compile(r"user_[a-zA-Z0-9]{20,}")
SEALED_PREFIX = "cursor-safe-storage:v1:"

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


class TokenUnsealError(Exception):
    """Cursor sealed the session and it could not be decrypted on this PC."""


_os_crypt_key: bytes | None = None


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", c_ulong), ("pbData", POINTER(c_char))]


class _BcryptAuthInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", c_ulong),
        ("dwInfoVersion", c_ulong),
        ("pbNonce", POINTER(c_ubyte)),
        ("cbNonce", c_ulong),
        ("pbAuthData", POINTER(c_ubyte)),
        ("cbAuthData", c_ulong),
        ("pbTag", POINTER(c_ubyte)),
        ("cbTag", c_ulong),
        ("pbMacContext", POINTER(c_ubyte)),
        ("cbMacContext", c_ulong),
        ("cbAAD", c_ulong),
        ("cbData", c_ulonglong),
        ("dwFlags", c_ulong),
    ]


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        POINTER(_DataBlob),
        POINTER(c_void_p),
        POINTER(_DataBlob),
        c_void_p,
        c_void_p,
        c_ulong,
        POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [c_void_p]
    kernel32.LocalFree.restype = c_void_p
    buffer = ctypes.create_string_buffer(data)
    blob_in = _DataBlob(len(data), cast(buffer, POINTER(c_char)))
    blob_out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        byref(blob_in), None, None, None, None, 0, byref(blob_out)
    ):
        raise TokenUnsealError(f"dpapi {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _cursor_os_crypt_key() -> bytes:
    global _os_crypt_key
    if _os_crypt_key is not None:
        return _os_crypt_key
    path = cursor_appdata() / "Local State"
    try:
        local = json.loads(path.read_text(encoding="utf-8"))
        encrypted_key = base64.b64decode(local["os_crypt"]["encrypted_key"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise TokenUnsealError("missing os_crypt key") from exc
    if encrypted_key.startswith(b"DPAPI"):
        encrypted_key = encrypted_key[5:]
    _os_crypt_key = _dpapi_unprotect(encrypted_key)
    return _os_crypt_key


def _aes_gcm_decrypt(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    if len(payload) < 16:
        raise TokenUnsealError("ciphertext too short")
    ciphertext, tag = payload[:-16], payload[-16:]
    bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        POINTER(c_void_p),
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        c_ulong,
    ]
    bcrypt.BCryptOpenAlgorithmProvider.restype = c_ulong
    bcrypt.BCryptSetProperty.argtypes = [
        c_void_p,
        ctypes.c_wchar_p,
        POINTER(c_ubyte),
        c_ulong,
        c_ulong,
    ]
    bcrypt.BCryptSetProperty.restype = c_ulong
    bcrypt.BCryptGetProperty.argtypes = [
        c_void_p,
        ctypes.c_wchar_p,
        POINTER(c_ubyte),
        c_ulong,
        POINTER(c_ulong),
        c_ulong,
    ]
    bcrypt.BCryptGetProperty.restype = c_ulong
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        c_void_p,
        POINTER(c_void_p),
        POINTER(c_ubyte),
        c_ulong,
        POINTER(c_ubyte),
        c_ulong,
        c_ulong,
    ]
    bcrypt.BCryptGenerateSymmetricKey.restype = c_ulong
    bcrypt.BCryptDecrypt.argtypes = [
        c_void_p,
        POINTER(c_ubyte),
        c_ulong,
        c_void_p,
        POINTER(c_ubyte),
        c_ulong,
        POINTER(c_ubyte),
        c_ulong,
        POINTER(c_ulong),
        c_ulong,
    ]
    bcrypt.BCryptDecrypt.restype = c_ulong
    bcrypt.BCryptDestroyKey.argtypes = [c_void_p]
    bcrypt.BCryptDestroyKey.restype = c_ulong
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [c_void_p, c_ulong]
    bcrypt.BCryptCloseAlgorithmProvider.restype = c_ulong

    def _check(status: int, step: str) -> None:
        if status != 0:
            raise TokenUnsealError(f"{step} {status:#x}")

    algorithm = c_void_p()
    _check(
        bcrypt.BCryptOpenAlgorithmProvider(byref(algorithm), "AES", None, 0),
        "open",
    )
    try:
        chain = "ChainingModeGCM\0".encode("utf-16le")
        chain_buf = (c_ubyte * len(chain)).from_buffer_copy(chain)
        _check(
            bcrypt.BCryptSetProperty(
                algorithm, "ChainingMode", chain_buf, len(chain), 0
            ),
            "chain",
        )
        object_len = c_ulong()
        written = c_ulong()
        _check(
            bcrypt.BCryptGetProperty(
                algorithm,
                "ObjectLength",
                cast(byref(object_len), POINTER(c_ubyte)),
                sizeof(object_len),
                byref(written),
                0,
            ),
            "object",
        )
        key_object = (c_ubyte * object_len.value)()
        key_handle = c_void_p()
        key_buf = (c_ubyte * len(key)).from_buffer_copy(key)
        _check(
            bcrypt.BCryptGenerateSymmetricKey(
                algorithm,
                byref(key_handle),
                key_object,
                object_len.value,
                key_buf,
                len(key),
                0,
            ),
            "key",
        )
        try:
            nonce_buf = (c_ubyte * len(nonce)).from_buffer_copy(nonce)
            tag_buf = (c_ubyte * len(tag)).from_buffer_copy(tag)
            info = _BcryptAuthInfo()
            info.cbSize = sizeof(_BcryptAuthInfo)
            info.dwInfoVersion = 1
            info.pbNonce = cast(nonce_buf, POINTER(c_ubyte))
            info.cbNonce = len(nonce)
            info.pbTag = cast(tag_buf, POINTER(c_ubyte))
            info.cbTag = len(tag)
            cipher_buf = (c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
            out_len = c_ulong()
            _check(
                bcrypt.BCryptDecrypt(
                    key_handle,
                    cipher_buf,
                    len(ciphertext),
                    byref(info),
                    None,
                    0,
                    None,
                    0,
                    byref(out_len),
                    0,
                ),
                "size",
            )
            output = (c_ubyte * out_len.value)()
            _check(
                bcrypt.BCryptDecrypt(
                    key_handle,
                    cipher_buf,
                    len(ciphertext),
                    byref(info),
                    None,
                    0,
                    output,
                    out_len.value,
                    byref(out_len),
                    0,
                ),
                "decrypt",
            )
            return bytes(output[: out_len.value])
        finally:
            bcrypt.BCryptDestroyKey(key_handle)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


def unseal_stored_secret(value: str | None) -> str | None:
    """Return a Cursor auth value, decrypting safeStorage seals from Cursor 3.27+."""
    if not value:
        return None
    if not value.startswith(SEALED_PREFIX):
        return value
    try:
        blob = base64.b64decode(value[len(SEALED_PREFIX) :])
    except ValueError as exc:
        raise TokenUnsealError("bad seal") from exc
    if len(blob) < 3 + 12 + 16 or blob[:3] not in (b"v10", b"v11"):
        raise TokenUnsealError("unexpected seal")
    plain = _aes_gcm_decrypt(_cursor_os_crypt_key(), blob[3:15], blob[15:])
    try:
        text = plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TokenUnsealError("bad plaintext") from exc
    if not text:
        raise TokenUnsealError("empty")
    return text


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
    access_token = unseal_stored_secret(read_db_value("cursorAuth/accessToken"))
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
    try:
        cookie = get_session_cookie()
    except TokenUnsealError:
        return UsageSnapshot(
            label="Locked",
            tooltip="Cursor session is encrypted and could not be unlocked on this PC.",
            max_ratio=None,
            error="decrypt",
        )
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
    if snapshot.error == "decrypt":
        return "Cursor: Sitzung gesperrt"
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
