"""Focus Google Meet and send its caption shortcut on the Linux Bot Screen."""

import ctypes
import os
import re
import subprocess
from contextlib import contextmanager, suppress
from pathlib import Path

from tools.bot_desktop import lease


_FOCUS_POINT = 20
_FOCUS_TARGET = "hermes-meet-caption-focus"


def _owns_process(pid: int) -> bool:
    while pid > 1:
        parent = Path(f"/proc/{pid}/status").read_text().splitlines()
        pid = int(next(line.split()[1] for line in parent if line.startswith("PPid:")))
        if pid == os.getpid():
            return True
    return False


def _active_meet_window(title: str) -> str:
    active = subprocess.check_output(["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True, timeout=1)
    window = re.search(r"window id # (0x[0-9a-fA-F]+)", active)
    if not window or int(window[1], 16) == 0:
        raise RuntimeError("No active window on Bot Screen.")
    props = subprocess.check_output(
        ["xprop", "-id", window[1], "_NET_WM_NAME", "_NET_WM_PID"], text=True, timeout=1,
    )
    window_title = re.search(r'^_NET_WM_NAME\(UTF8_STRING\) = "(.+)"$', props, re.M)
    pid = re.search(r"^_NET_WM_PID\(CARDINAL\) = ([1-9][0-9]*)$", props, re.M)
    if (not title or not window_title or not pid
            or not (window_title[1] == title or window_title[1].startswith(title + " - "))
            or not _owns_process(int(pid[1]))):
        raise RuntimeError("The active Bot Screen window does not belong to this Meet tab.")
    return window[1]


def _verify_click_position(window: str, x: int, y: int) -> None:
    info = subprocess.check_output(["xwininfo", "-id", window], text=True, timeout=1)
    fields = dict(re.findall(r"^\s*(Absolute upper-left [XY]|Width|Height):\s*(-?\d+)\s*$", info, re.M))
    left, top, width, height = (int(fields[key]) for key in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height"))
    if not (left <= x < left + width and top <= y < top + height):
        raise RuntimeError("The Meet focus point is outside its Bot Screen window.")


@contextmanager
def _native_input():
    x11 = ctypes.CDLL("libX11.so.6")
    xtest = ctypes.CDLL("libXtst.so.6")
    ptr, integer, unsigned, delay = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], ptr
    x11.XCloseDisplay.argtypes = [ptr]
    x11.XSync.argtypes = [ptr, integer]
    x11.XKeysymToKeycode.argtypes = [ptr, delay]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    xtest.XTestFakeMotionEvent.argtypes = [ptr, integer, integer, integer, delay]
    xtest.XTestFakeButtonEvent.argtypes = [ptr, unsigned, integer, delay]
    xtest.XTestFakeKeyEvent.argtypes = [ptr, unsigned, integer, delay]
    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("Could not open Bot Screen for the Meet caption shortcut.")
    try:
        yield x11, xtest, display
    finally:
        x11.XCloseDisplay(display)


def _click(x11, xtest, display, x: int, y: int) -> None:
    if not (xtest.XTestFakeMotionEvent(display, 0, x, y, 0)
            and xtest.XTestFakeButtonEvent(display, 1, 1, 0)):
        raise RuntimeError("Could not focus the Meet page with native input.")
    xtest.XTestFakeButtonEvent(display, 1, 0, 0)
    x11.XSync(display, 0)


def _send_c(x11, xtest, display) -> None:
    key = x11.XKeysymToKeycode(display, ord("c"))
    if not key or not xtest.XTestFakeKeyEvent(display, key, 1, 0):
        raise RuntimeError("Could not send the Meet caption shortcut.")
    xtest.XTestFakeKeyEvent(display, key, 0, 0)
    x11.XSync(display, 0)


def press_caption_shortcut(page) -> None:
    admitted = lease.assert_agent_may_act()
    page.bring_to_front()
    title = page.title()
    window = _active_meet_window(title)
    # Learn the viewport's screen offset without guessing Chrome's toolbar height.
    # The temporary target lets us confirm where the physical click actually lands.
    page.evaluate("""id => {
        const target = document.createElement('div');
        target.id = id;
        target.style.cssText = 'all:initial;position:fixed;top:8px;left:8px;width:24px;height:24px;z-index:2147483647;opacity:0';
        target.addEventListener('pointermove', event => {
            target.dataset.screenPoint = JSON.stringify([event.screenX, event.screenY, devicePixelRatio]);
        });
        target.addEventListener('pointerdown', () => { target.dataset.clicked = 'true'; });
        document.body.append(target);
    }""", _FOCUS_TARGET)
    try:
        page.mouse.move(_FOCUS_POINT, _FOCUS_POINT)
        screen_x, screen_y, scale = page.wait_for_function(
            "id => JSON.parse(document.getElementById(id)?.dataset.screenPoint ?? 'null')",
            arg=_FOCUS_TARGET, timeout=2_000,
        ).json_value()
        x, y = round(screen_x * scale), round(screen_y * scale)
        _verify_click_position(window, x, y)
        if _active_meet_window(title) != window or lease.assert_agent_may_act().epoch != admitted.epoch:
            raise RuntimeError("Bot Screen focus or control changed before the caption shortcut.")
        with _native_input() as (x11, xtest, display):
            _click(x11, xtest, display, x, y)
            page.wait_for_function(
                "id => document.hasFocus() && document.getElementById(id)?.dataset.clicked === 'true'",
                arg=_FOCUS_TARGET, timeout=2_000,
            )
            if _active_meet_window(title) != window or lease.assert_agent_may_act().epoch != admitted.epoch:
                raise RuntimeError("Bot Screen focus or control changed before the caption shortcut.")
            _send_c(x11, xtest, display)
    finally:
        with suppress(Exception):
            page.evaluate("id => document.getElementById(id)?.remove()", _FOCUS_TARGET)
