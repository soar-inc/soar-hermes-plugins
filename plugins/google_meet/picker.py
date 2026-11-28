"""Operate the Chromium tab chooser through the existing computer_use driver."""

from __future__ import annotations

import base64
import ctypes
from ctypes.util import find_library
import json
import re
import subprocess
import time

class PickerError(RuntimeError):
    """The native chooser could not be operated safely."""


class PickerDismissed(PickerError):
    """A failed chooser was cancelled and the share can be retried."""


def active_window(expected_title: str | None = None) -> tuple[int, int]:
    active = subprocess.check_output(
        ["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True, timeout=1,
    )
    window = re.search(r"window id # (0x[0-9a-fA-F]+)", active)
    if window is None or int(window[1], 16) == 0:
        raise PickerError("Couldn't identify the Meet browser window.")
    properties = subprocess.check_output(
        ["xprop", "-id", window[1], "_NET_WM_PID", "_NET_WM_NAME"], text=True, timeout=1,
    )
    pid = re.search(r"^_NET_WM_PID\(CARDINAL\) = ([1-9][0-9]*)$", properties, re.M)
    if pid is None:
        raise PickerError("Couldn't identify the Meet browser process.")
    if expected_title:
        from plugins.google_meet.caption_shortcut import _owns_process

        title = re.search(r'^_NET_WM_NAME\(UTF8_STRING\) = "([^"]+)"$', properties, re.M)
        if (title is None or title[1].split(" - ", 1)[0] != expected_title
                or not _owns_process(int(pid[1]))):
            raise PickerError("Couldn't confirm the dedicated Meet browser window.")
    return int(pid[1]), int(window[1], 16)


def _picker_points(rgb: bytes, width: int, height: int):
    """Find the light-theme Chromium Tab pane and its buttons from a window capture."""
    if width < 610 or height < 540 or len(rgb) != width * height * 3:
        return None
    blue, pane, white = b"\x0b\x57\xd0", b"\xed\xf2\xfa", b"\xff\xff\xff"

    def pixel(x, y):
        offset = 3 * (y * width + x)
        return rgb[offset:offset + 3]

    matches = []
    # The X11 driver does not expose Chromium's native chooser controls in AX.
    # Anchor on the selected Chromium Tab underline and the panel below it.
    for y in range(2, height - 408):
        for x in range(width // 2 - 288, width // 2 - 279):
            if (pixel(x, y) != blue or pixel(x - 1, y) == blue
                    or pixel(x + 206, y) != blue or pixel(x + 207, y) == blue
                    or pixel(x - 1, y + 1) == pane or pixel(x, y + 1) != pane
                    or pixel(x + 567, y + 10) != pane or pixel(x + 568, y + 10) != white
                    or pixel(x + 5, y + 54) != pane or pixel(x + 5, y + 350) != white):
                continue
            matches.append((x, y))
    if len(matches) != 1:
        return None
    x, y = matches[0]
    # An empty chooser and a populated, unselected chooser have different gray fills.
    share_fill = pixel(x + 506, y + 379)
    if share_fill not in (blue, b"\xe5\xe5\xe5", b"\xe7\xe7\xe7"):
        return None
    row_visible = any(min(pixel(x + dx, y + dy)) < 160
                      for dx in range(28, 170, 2) for dy in range(28, 44, 2))
    return (x + 55, y + 35), (x + 439, y + 385), (x + 527, y + 385), share_fill == blue, row_visible


class _PngImage(ctypes.Structure):
    # libpng's public simplified API, png_image (PNG_IMAGE_VERSION = 1).
    _fields_ = [
        ("opaque", ctypes.c_void_p), ("version", ctypes.c_uint32),
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("format", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("colormap_entries", ctypes.c_uint32), ("warning_or_error", ctypes.c_uint32),
        ("message", ctypes.c_char * 64),
    ]


def _png_rgb(data: bytes, width: int, height: int) -> bytes:
    library = find_library("png16")
    if library is None:
        raise PickerError("Bot Screen can't decode the Chrome picker screenshot.")
    png = ctypes.CDLL(library)
    state = _PngImage(version=1)
    state_ptr = ctypes.byref(state)
    png.png_image_begin_read_from_memory.argtypes = [
        ctypes.POINTER(_PngImage), ctypes.c_char_p, ctypes.c_size_t,
    ]
    png.png_image_finish_read.argtypes = [
        ctypes.POINTER(_PngImage), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
    ]
    png.png_image_free.argtypes = [ctypes.POINTER(_PngImage)]
    png.png_image_free.restype = None
    try:
        if (not png.png_image_begin_read_from_memory(state_ptr, data, len(data))
                or (state.width, state.height) != (width, height)
                or not (610 <= width <= 4096 and 540 <= height <= 4096)):
            return b""
        state.format = 2  # PNG_FORMAT_RGB
        out = ctypes.create_string_buffer(width * height * 3)
        if png.png_image_finish_read(state_ptr, None, out, 0, None):
            return out.raw
        return b""
    finally:
        png.png_image_free(state_ptr)


def _capture_picker(backend, window):
    pid, window_id = window
    image = backend.capture(mode="vision", pid=pid, window_id=window_id)
    if not image.png_b64:
        return None
    rgb = _png_rgb(base64.b64decode(image.png_b64), image.width, image.height)
    return _picker_points(rgb, image.width, image.height)


def select_tab(handler, session: str, window: tuple[int, int], still_selecting, *, dismiss_only=False) -> bool:
    from tools.approval import approve_session, clear_session
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.bot_desktop import lease
    from tools.computer_use.tool import _backend_for_call, release_computer_use_session

    epoch = lease.assert_agent_may_act().epoch
    admission = set_current_session_key(session)

    def check():
        if (lease.assert_agent_may_act().epoch != epoch or not still_selecting()
                or active_window() != window):
            raise PickerError("The Meet picker lost focus or the share request expired.")

    def inspect():
        check()
        with _backend_for_call(session) as backend:
            points = _capture_picker(backend, window)
        check()
        return points

    def click(point):
        check()
        response = handler({"action": "click", "coordinate": list(point),
                            "delivery_mode": "foreground"}, session_id=session)
        result = json.loads(response) if isinstance(response, str) else response
        if not isinstance(result, dict) or not result.get("ok"):
            raise PickerError("computer_use couldn't click the Chrome tab picker.")

    def wait_for(predicate):
        for _ in range(3):
            points = inspect()
            if points is not None and predicate(points):
                return points
            time.sleep(0.15)
        return None

    try:
        approve_session(session, "cua:click:foreground")
        if dismiss_only:
            if points := inspect():
                click(points[1])
                return True
            return False
        try:
            points = wait_for(lambda points: not points[3] and points[4])
            if points is None:
                raise PickerError("Chrome didn't show the requested tab in its chooser.")
            click(points[0])
            points = wait_for(lambda points: points[3])
            if points is None:
                raise PickerError("Chrome didn't confirm the requested tab was selected.")
            click(points[2])
            return True
        except Exception as exc:
            # Re-capture under the same lease, even if the first capture was unknown.
            try:
                points = inspect()
                if points is not None:
                    click(points[1])
            except Exception:
                points = None
            if points is not None:
                raise PickerDismissed(str(exc)) from exc
            raise
    finally:
        release_computer_use_session(session)
        clear_session(session)
        reset_current_session_key(admission)
