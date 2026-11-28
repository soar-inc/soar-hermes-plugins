"""Resolve Google Meet voice tools and scope desktop access to the shared tab."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from threading import Lock

PRESENTATION_TOOLSET = "meet-presentation"
_COMPUTER_USE = "computer_use"
_SHARED_TAB_INSTRUCTIONS = (
    " For a requested click, capture the actively shared page first and use "
    "a page control from that capture. If it has no number, capture with its exact visible "
    "label to get screenshot coordinates, then click using that label and the coordinates. "
    "Capture the shared tab before scrolling or typing. For typing, pass the field label, "
    "or omit it to use the already focused editable field. To show another URL, call share_tab "
    "directly and keep the current presentation running. When asked to go to a named section of "
    "the shared page, use scroll_to_section with that section name."
)


def uses_computer_use_picker(home: Path) -> bool:
    return home.name == "long-horizon" and home.parent.name == "profiles"


def _is_point(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(type(item) is int and item >= 0 for item in value)


@contextmanager
def voice_tools(config: dict, home: Path, presentation) -> Iterator[tuple[list[str], str]]:
    """Resolve the voice-turn toolsets and keep the shared-tab guard active for the turn."""
    from hermes_cli.tools_config import _get_platform_tools

    base_toolsets = ["mcp-flowmind", PRESENTATION_TOOLSET]
    if (
        uses_computer_use_picker(home)
        and _COMPUTER_USE in _get_platform_tools(config, "cli")
    ):
        with _shared_tab_computer_use(presentation) as restricted:
            if restricted:
                yield [*base_toolsets, _COMPUTER_USE], _SHARED_TAB_INSTRUCTIONS
                return
    yield base_toolsets, ""


@contextmanager
def _shared_tab_computer_use(presentation):
    """Scope the existing tool to this voice turn in the detached Meet process."""
    from tools.approval import approve_session, clear_session
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.computer_use_tool import release_computer_use_session
    from tools.registry import registry

    entry = registry.get_entry(_COMPUTER_USE)
    if entry is None:
        yield False
        return

    original = entry.handler
    original_schema = entry.schema
    backend_session = f"meet-share:{presentation.capture_key}"
    captured_target = None
    captured_size = None

    def foreground(args):
        token = set_current_session_key(backend_session)
        try:
            approve_session(backend_session, f"cua:{args['action']}:foreground")
            return original(args, session_id=backend_session)
        finally:
            reset_current_session_key(token)

    def run_on_shared_tab(args, **kwargs):
        nonlocal captured_target, captured_size
        action = args.get("action")
        if action == "share_tab" and presentation.native_picker:
            from plugins.google_meet.picker import select_tab

            return presentation.request("computer-use-share", {
                "url": args.get("url"),
                "title": args.get("title"),
                "picker": partial(select_tab, original, f"{backend_session}:picker"),
            })
        if action == "scroll_to_section" and presentation.native_picker:
            section = args.get("label")
            if not isinstance(section, str) or not 0 < len(section.strip()) <= 120:
                return json.dumps({"error": "Give the name of the section on the shared page."})
            captured_target, captured_size = None, None
            return presentation.request("computer-use-section", {"label": section.strip()})
        if action in ("scroll", "type") and presentation.native_picker:
            if captured_target is None:
                return json.dumps({"error": "Capture the current shared tab first."})
            request = {"action": action, "target": captured_target, "size": captured_size, "input": foreground}
            if action == "scroll":
                direction, amount = args.get("direction", "down"), args.get("amount", 3)
                point = args.get("coordinate")
                if direction not in ("up", "down", "left", "right") or type(amount) is not int or not 1 <= amount <= 12:
                    return json.dumps({"error": "Scroll needs a direction and an amount between 1 and 12."})
                if point is not None and (captured_size is None or not _is_point(point)):
                    return json.dumps({"error": "Use [x,y] from the shared-tab screenshot for a scroll point."})
                request.update(direction=direction, amount=amount, point=point)
            else:
                text, label = args.get("text"), args.get("label")
                if (not isinstance(text, str) or not 0 < len(text) <= 1000
                        or not text.isprintable()):
                    return json.dumps({"error": "Type accepts up to 1000 printable characters on one line."})
                if label is not None and (not isinstance(label, str) or not 0 < len(label.strip()) <= 120):
                    return json.dumps({"error": "Use the visible label of the text field."})
                request.update(text=text, label=label.strip() if label else None)
            captured_target, captured_size = None, None
            return presentation.request("computer-use-shared-input", request)

        if action not in ("capture", "click"):
            return json.dumps({"error": "While presenting, this tool can capture the shared page and click its controls."})
        element = args.get("element")
        coordinate = args.get("coordinate")
        if action == "click" and captured_target is None:
            return json.dumps({"error": "Capture the shared page before clicking."})
        if action == "click" and coordinate is not None:
            label = args.get("label")
            if (element is not None or captured_size is None
                    or not _is_point(coordinate)
                    or not isinstance(label, str) or not label.strip() or len(label) > 120):
                return json.dumps({"error": "Use the fresh screenshot's [x,y] pixels and the control's visible label."})
            target, size = captured_target, captured_size
            captured_target, captured_size = None, None

            return presentation.request("computer-use-coordinate", {
                "target": target, "size": size, "point": coordinate,
                "label": label.strip(), "click": foreground,
            })
        if action == "click" and (type(element) is not int or element < 1):
            return json.dumps({"error": "Choose a numbered control or use its label and screenshot coordinates."})

        target = json.loads(presentation.request("computer-use-target", {}))
        if not target.get("ok"):
            captured_target = None
            return json.dumps(target)
        identity = (target["pid"], target["window_id"], target["tab"])
        kwargs["session_id"] = backend_session
        if action == "click":
            previous, captured_target = captured_target, None
            if (previous["pid"], previous["window_id"], previous["tab"]) != identity:
                return json.dumps({"error": "The shared tab changed. Capture it again before clicking."})
            # The operator enabled clicks on the active share. Use Hermes's own grant
            # for this private session and this action, with the stock lease checks.
            approval_token = set_current_session_key(backend_session)
            try:
                approve_session(backend_session, "cua:click:background")
                return original({"action": "click", "element": element}, **kwargs)
            finally:
                reset_current_session_key(approval_token)

        captured_target, captured_size = None, None
        response = original({"action": "capture", "mode": "som", "pid": target["pid"],
                             "window_id": target["window_id"]}, **kwargs)
        result = json.loads(response) if isinstance(response, str) else response
        if isinstance(result, dict) and (result.get("_multimodal") or result.get("mode") == "som"):
            captured_target = target
            meta = result.get("meta") or result
            if (result.get("_multimodal") or meta.get("screenshot_path")) and all(
                    type(meta.get(k)) is int and meta[k] > 0 for k in ("width", "height")):
                captured_size = (meta["width"], meta["height"])
        label = args.get("label")
        if captured_size is not None and isinstance(label, str) and 0 < len(label.strip()) <= 120:
            hint = json.loads(presentation.request("computer-use-control-point", {
                "target": captured_target, "size": captured_size, "label": label.strip(),
            }))
            if isinstance(response, dict) and response.get("_multimodal"):
                text = "Shared-page control position: " + json.dumps(hint)
                return {**response, "content": [*response["content"], {"type": "text", "text": text}],
                        "text_summary": response.get("text_summary", "") + "\n" + text}
            return json.dumps({**result, "shared_page_control": hint})
        return response

    lock = Lock()

    def restricted(args, **kwargs):
        with lock:
            return run_on_shared_tab(args, **kwargs)

    if presentation.native_picker:
        entry.schema = {
            "name": _COMPUTER_USE,
            "description": (
                "Show a requested HTTPS page in Dev's current Google Meet with share_tab. When Dev is "
                "already sharing, share_tab opens the new URL in the same tab without stopping the share. "
                "Use a URL supplied by the participant or verified by a lookup. "
                "On the shared page, capture before clicking. Prefer an element number. If the control "
                "has no number, capture with the exact visible label to locate its screenshot [x,y] pixels. "
                "Use those coordinates and the label for click. After capture, scroll the shared page "
                "with direction and optional amount or screenshot point. To type text, specify the "
                "field label or use an already focused editable field. Use scroll_to_section and "
                "label to bring a named section of the shared page into view."
            ),
            "parameters": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {"type": "string", "enum": ["share_tab", "capture", "click", "scroll", "scroll_to_section", "type"]},
                    "url": {"type": "string", "description": "HTTPS destination required for share_tab."},
                    "title": {"type": "string", "description": "Optional display name for the shared page."},
                    "element": {"type": "integer", "description": "Numbered page control."},
                    "coordinate": {
                        "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
                        "description": "Screenshot [x,y] point for a coordinate click or to aim a scroll.",
                    },
                    "label": {"type": "string", "description": "Visible control, editable field, or section name."},
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "amount": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Scroll ticks, default 3."},
                    "text": {"type": "string", "maxLength": 1000, "description": "Literal single-line text to type."},
                },
                "additionalProperties": False,
            },
        }
    entry.handler = restricted
    try:
        yield True
    finally:
        entry.handler = original
        entry.schema = original_schema
        release_computer_use_session(backend_session)
        clear_session(backend_session)
