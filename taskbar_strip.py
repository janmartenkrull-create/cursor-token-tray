"""Taskbar-docked widget for Windows 11 (no deskband support)."""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import font as tkfont

from cursor_token_tray import (
    POLL_SECONDS,
    UsageSnapshot,
    _error_headline,
    _value_color,
    _value_text,
    fetch_usage,
)

STRIP_WIDTH = 256
TRAY_GAP = 8
KEEP_ALIVE_MS = 200
REPOSITION_MS = 1500
CHROMA = "#ff00ff"

GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200

user32 = ctypes.windll.user32
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
user32.GetWindowLongPtrW.restype = ctypes.c_void_p
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = ctypes.c_bool


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _taskbar_hwnd() -> int:
    return int(user32.FindWindowW("Shell_TrayWnd", None) or 0)


def _taskbar_rect() -> RECT | None:
    hwnd = _taskbar_hwnd()
    if not hwnd:
        return None
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect


def _tray_notify_left() -> int | None:
    tray = _taskbar_hwnd()
    if not tray:
        return None
    notify = user32.FindWindowExW(tray, 0, "TrayNotifyWnd", None)
    if not notify:
        return None
    rect = RECT()
    if not user32.GetWindowRect(notify, ctypes.byref(rect)):
        return None
    return rect.left


def _uses_light_taskbar() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            return bool(value)
    except OSError:
        return False


def _rgb_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


class TaskbarStripApp:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._light = _uses_light_taskbar()
        if self._light:
            self._label_fg = "#4b5563"
            self._track = "#9ca3af"
        else:
            self._label_fg = "#d1d5db"
            self._track = "#6b7280"

        self._last_geom = ""
        self._hwnd = 0
        self._styles_applied = False
        self._menu_open = False
        self._snapshot = UsageSnapshot("…", "Loading…", None)
        self._height = 48

        self.root = tk.Tk()
        self.root.title("Cursor Token Usage")
        self.root.overrideredirect(True)
        self.root.configure(bg=CHROMA)
        self.root.wm_attributes("-toolwindow", True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", CHROMA)

        self._caption_font = tkfont.Font(family="Segoe UI", size=7)
        self._value_font = tkfont.Font(family="Segoe UI", size=12, weight="bold")

        self.canvas = tk.Canvas(
            self.root,
            width=STRIP_WIDTH,
            height=self._height,
            bg=CHROMA,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Configure>", self._on_configure)

        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="Jetzt aktualisieren", command=self._refresh_now)
        self._menu.add_separator()
        self._menu.add_command(label="Beenden", command=self._quit)
        self._menu.bind("<Unmap>", self._on_menu_closed)

        self.root.update_idletasks()
        self._hwnd = self._toplevel_hwnd()
        self._apply_window_styles()
        self._reposition()
        self._draw()
        self.root.after(KEEP_ALIVE_MS, self._keep_alive)
        self.root.after(REPOSITION_MS, self._reposition_loop)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _toplevel_hwnd(self) -> int:
        child = int(self.root.winfo_id())
        parent = user32.GetParent(child)
        return int(parent or child)

    def _apply_window_styles(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        if not self._styles_applied:
            style = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0)
            style |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
            tray = _taskbar_hwnd()
            if tray:
                user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, tray)
            self._styles_applied = True

        if self._menu_open:
            return
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
        )

    def _show_menu(self, event: tk.Event) -> None:
        self._menu_open = True
        self._menu.update_idletasks()
        menu_h = self._menu.winfo_reqheight() or 72
        x = event.x_root
        y = self.root.winfo_rooty() - menu_h - 8
        if y < 0:
            y = 0
        self._menu.post(x, y)
        self.root.update_idletasks()
        self._raise_menu()

    def _raise_menu(self) -> None:
        try:
            hwnd = int(self._menu.winfo_id())
            parent = int(user32.GetParent(hwnd) or 0)
            target = parent or hwnd
            user32.SetWindowPos(
                target,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except OSError:
            pass

    def _on_menu_closed(self, _event: tk.Event | None = None) -> None:
        self._menu_open = False

    def _refresh_now(self) -> None:
        threading.Thread(target=self._fetch_and_apply, daemon=True).start()

    def _quit(self) -> None:
        self._stop.set()
        self.root.quit()

    def _on_configure(self, event: tk.Event) -> None:
        if event.height > 1 and event.height != self._height:
            self._height = event.height
            self._draw()

    def _desired_geometry(self) -> str | None:
        rect = _taskbar_rect()
        if not rect:
            return None
        height = max(40, rect.bottom - rect.top)
        width = STRIP_WIDTH
        notify_left = _tray_notify_left()
        if notify_left is not None:
            x = notify_left - width - TRAY_GAP
        else:
            x = rect.right - width - 320
        y = rect.top
        if x < rect.left:
            x = rect.left
        return f"{width}x{height}+{x}+{y}"

    def _reposition(self) -> None:
        geom = self._desired_geometry()
        if not geom or geom == self._last_geom:
            return
        self._last_geom = geom
        self.root.geometry(geom)

    def _keep_alive(self) -> None:
        if self._stop.is_set():
            return
        if not user32.IsWindow(self._hwnd):
            self._hwnd = self._toplevel_hwnd()
            self._styles_applied = False
        if not self._menu_open:
            self.root.attributes("-topmost", True)
            self._apply_window_styles()
        self.root.after(KEEP_ALIVE_MS, self._keep_alive)

    def _reposition_loop(self) -> None:
        if self._stop.is_set():
            return
        self._reposition()
        self.root.after(REPOSITION_MS, self._reposition_loop)

    def _draw_pill(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        label: str,
        value: str,
        color: tuple[int, int, int],
        ratio: float | None,
    ) -> None:
        accent = _rgb_hex(color)
        cx = (x1 + x2) / 2
        bar_h = 3
        bar_y = y2 - 7
        label_y = y1 + 3
        value_y = (label_y + 11 + bar_y - 3) / 2
        self.canvas.create_text(
            cx,
            label_y,
            text=label.upper(),
            fill=self._label_fg,
            font=self._caption_font,
            anchor="n",
        )
        self.canvas.create_text(
            cx,
            value_y,
            text=value,
            fill=accent,
            font=self._value_font,
            anchor="center",
        )
        bar_x1 = x1 + 12
        bar_x2 = x2 - 12
        self.canvas.create_rectangle(bar_x1, bar_y, bar_x2, bar_y + bar_h, fill=self._track, outline="")
        if ratio is not None and ratio > 0:
            filled = bar_x1 + max(3, (bar_x2 - bar_x1) * min(1.0, ratio))
            self.canvas.create_rectangle(bar_x1, bar_y, filled, bar_y + bar_h, fill=accent, outline="")

    def _draw(self) -> None:
        c = self.canvas
        w = STRIP_WIDTH
        h = self._height
        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill=CHROMA, outline="")

        snapshot = self._snapshot
        pad = 4
        inner_y1 = pad
        inner_y2 = h - pad

        if snapshot.error:
            c.create_text(
                w / 2,
                h / 2,
                text=_error_headline(snapshot),
                fill=_rgb_hex((230, 120, 120)),
                font=self._caption_font,
            )
            return

        if snapshot.display_mode == "overall":
            ratio = snapshot.max_ratio
            self._draw_pill(
                pad,
                inner_y1,
                w - pad,
                inner_y2,
                "Included",
                _value_text(snapshot, "cursor"),
                _value_color(snapshot, "cursor"),
                ratio,
            )
            return

        gap = 6
        mid = w / 2
        cursor_ratio = None if snapshot.is_unlimited else (
            (snapshot.cursor_pct or 0) / 100.0 if snapshot.cursor_pct is not None else None
        )
        other_ratio = None if snapshot.is_unlimited else (
            (snapshot.other_pct or 0) / 100.0 if snapshot.other_pct is not None else None
        )
        self._draw_pill(
            pad,
            inner_y1,
            mid - gap / 2,
            inner_y2,
            "Cursor",
            _value_text(snapshot, "cursor"),
            _value_color(snapshot, "cursor"),
            cursor_ratio,
        )
        self._draw_pill(
            mid + gap / 2,
            inner_y1,
            w - pad,
            inner_y2,
            "Other",
            _value_text(snapshot, "other"),
            _value_color(snapshot, "other"),
            other_ratio,
        )

    def _apply_snapshot(self, snapshot: UsageSnapshot) -> None:
        self._snapshot = snapshot
        self._draw()

    def _fetch_and_apply(self) -> None:
        snapshot = fetch_usage()
        if not self._stop.is_set():
            self.root.after(0, lambda: self._apply_snapshot(snapshot))

    def _poll_loop(self) -> None:
        self._fetch_and_apply()
        while not self._stop.wait(POLL_SECONDS):
            self._fetch_and_apply()

    def run(self) -> None:
        self.root.mainloop()


def run_taskbar_strip() -> None:
    TaskbarStripApp().run()
