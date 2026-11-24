"""Headless Soar Meet participant. Soar owns admission and caption access."""

import argparse
import contextlib
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from hermes_cli.browser_runtime import chromium_executable
from plugins.google_meet.audio_bridge import AudioBridge
from plugins.google_meet.meet_bot import _BotState, _STATUS_FIELDS, _start_realtime_speaker, _teardown_realtime
from plugins.google_meet.wake_caption import CaptionWake
from utils import atomic_json_write

from .session import ORIGIN, identity, meeting_url, read_json, workspace

_FIRST_PARTICIPANT_WAIT_S = 10 * 60


# Match the original English caption rows from CaptionStrip. Translated rows are
# wrapped in divs. React retains the original paragraph as it revises a caption.
_CAPTIONS = """
() => {
    if (!window.__hermesSoarRows) window.__hermesSoarRows = { next: 0, seen: new WeakMap() };
    const state = window.__hermesSoarRows;
    const log = document.querySelector('section[aria-label="Live captions"] [role="log"]');
    if (!log) return { open: false, entries: [] };
    const entries = [];
    for (const row of log.querySelectorAll(':scope > p[lang]')) {
        const parts = row.querySelectorAll(':scope > bdi');
        if (parts.length !== 2 || row.getAttribute('aria-hidden') === 'true') continue;
        const speaker = parts[0].textContent.trim();
        const text = parts[1].textContent.trim();
        if (!speaker || !text) continue;
        const previous = state.seen.get(row);
        if (previous && previous.speaker === speaker && previous.text === text) continue;
        const sameSpeaker = previous && previous.speaker === speaker;
        const number = sameSpeaker ? previous.row : ++state.next;
        state.seen.set(row, { row: number, speaker, text });
        entries.push({ row: number, speaker, text, previous: sameSpeaker ? previous.text : '' });
    }
    return { open: true, entries };
}
"""


class _SoarState(_BotState):
    signed_in_as = None

    def _flush(self):
        data = {key: getattr(self, attr) if attr else None for key, attr, _ in _STATUS_FIELDS}
        data.update(signedInAs=self.signed_in_as, transcriptPath=str(self.transcript_path), pid=os.getpid())
        atomic_json_write(self.status_path, data, mode=0o600)


def _button(page, label):
    return page.get_by_role("button", name=label, exact=True).first


def _click_if_visible(page, label):
    button = _button(page, label)
    if button.is_visible():
        button.click(timeout=3_000)
        return True
    return False


def _get_comms(page, path, *, timeout=5_000):
    response = page.request.get(f"{ORIGIN}/api/comms/v1/{path}", timeout=timeout, max_redirects=0)
    if not response.ok:
        raise ValueError(f"Soar Meet could not verify {path} (HTTP {response.status}).")
    return response.json()


def _ring_answer(page, conversations, target):
    name = (target.get("name") or "").strip()
    if not name:
        return None
    label = f"# {name}" if target.get("type") == "channel" else name
    # The toast exposes its conversation label, not its ID. Leave ambiguous
    # notices alone; the link or the banner can still join the right call.
    matching = [
        item for item in conversations
        if (f"# {(item.get('name') or '').strip()}" if item.get("type") == "channel"
            else (item.get("name") or "").strip()) == label
    ]
    if len(matching) != 1:
        return None
    body = page.get_by_text(f"Calling in \u2068{label}\u2069", exact=True)
    return page.get_by_role("status").filter(has=body).get_by_role(
        "button", name="Answer", exact=True,
    ).first


def _wait_for_call(page, url, state, stop, *, until=None, start_call=False):
    target_url = urlsplit(url)
    conversation_id = target_url.path.rsplit("/", 1)[-1]
    conversations = _get_comms(page, "conversations")
    if not isinstance(conversations, list):
        raise ValueError("Soar Meet did not return the conversation list.")
    target = next((item for item in conversations if item.get("id") == conversation_id), None)
    if target is None:
        raise ValueError("The signed-in Soar account is not a member of this conversation.")

    answer = _ring_answer(page, conversations, target)
    join = page.get_by_text("A call is in progress", exact=True).locator(
        "xpath=../..",
    ).get_by_role("button", name="Join", exact=True).first
    join_seconds = 45 if until is None else min(300, max(0, until - time.time()))
    deadline = time.monotonic() + join_seconds
    next_check = 0.0
    attempted = False
    started_at = None
    while not stop["stop"] and time.monotonic() < deadline:
        current_url = urlsplit(page.url)
        if current_url.netloc != target_url.netloc or current_url.path != target_url.path:
            raise ValueError("Soar Meet did not open the requested conversation.")
        if _button(page, "Leave call").is_visible():
            state.set(in_call=True, joined_at=time.time())
            return True, started_at
        if not attempted and not current_url.query and time.monotonic() >= next_check:
            # The app strips join=1 after it checks for an active call.
            # Read fresh state before opening or confirming the start prompt.
            active = _get_comms(page, "calls/active").get("calls")
            if not isinstance(active, list):
                raise ValueError("Soar Meet did not return the active call list.")
            next_check = time.monotonic() + 1
            if _button(page, "Leave call").is_visible():
                continue
            if any(call.get("conversationId") == conversation_id for call in active):
                for control in (answer, join):
                    if control is not None and control.is_visible() and control.is_enabled():
                        control.click(timeout=3_000)
                        attempted = True
                        break
            elif until is None or start_call:
                if _click_if_visible(page, "Call now"):
                    attempted = True
                    started_at = time.monotonic()
                else:
                    _click_if_visible(page, "Call")
        page.wait_for_timeout(250)
    if until is not None and time.time() >= until:
        state.set(leave_reason="scheduled_end")
    elif not stop["stop"]:
        state.set(error="Could not connect to a call in the requested Soar Meet conversation.")
    return False, None


def _call_occupancy(page, conversation_id):
    try:
        result = _get_comms(page, "calls/active", timeout=2_000)
    except (PlaywrightError, ValueError):
        return None, None
    calls = result.get("calls") if isinstance(result, dict) else None
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict) and call.get("conversationId") == conversation_id:
                return call.get("callId"), call.get("participants")
    return None, None


def _drain(page, state, realtime, wake, stop, *, conversation_id, wake_session=None, until=None,
           first_participant_deadline=None):
    listener = None
    if wake:
        listener = CaptionWake(
            wake, state.out_dir, wake_name="Dev", wake_aliases=("Horizon",), wake_session=wake_session,
        )
    state.set(wake_enabled=listener is not None)
    last_seen_in_call = time.monotonic()
    next_toggle = 0.0
    next_occupancy_check = 0.0
    current_call_id = None
    had_company = False
    alone_since = None
    primed = False
    while not stop["stop"] and not page.is_closed():
        if until is not None and time.time() >= until:
            state.set(leave_reason="scheduled_end")
            return
        now = time.monotonic()
        if _button(page, "Leave call").is_visible():
            last_seen_in_call = now
            if now >= next_occupancy_check:
                call_id, participants = _call_occupancy(page, conversation_id)
                checked_at = time.monotonic()
                next_occupancy_check = checked_at + 5
                if call_id and call_id != current_call_id:
                    current_call_id = call_id
                    had_company = False
                    alone_since = None
                known_count = bool(call_id) and type(participants) is int and participants >= 1
                if known_count and participants > 1:
                    had_company = True
                    first_participant_deadline = None
                elif (known_count and participants == 1 and first_participant_deadline is not None
                      and checked_at >= first_participant_deadline):
                    state.set(leave_reason="no_participants_timeout")
                    return
                # Only begin the timer after another participant has been present.
                # Missing occupancy or a reconnect resets the grace period.
                if had_company and known_count and participants == 1:
                    if alone_since is None:
                        alone_since = checked_at
                    elif checked_at - alone_since >= 15:
                        state.set(leave_reason="all_participants_left")
                        return
                else:
                    alone_since = None
        else:
            alone_since = None
            if now - last_seen_in_call > 5:
                state.set(leave_reason="call_ended")
                return
        batch = page.evaluate(_CAPTIONS)
        if not batch["open"]:
            primed = False
            if now >= next_toggle:
                next_toggle = now + 2
                if _click_if_visible(page, "Show captions"):
                    state.set(captions_enabled_attempted=True)
        else:
            if not state.captioning:
                state.set(captioning=True)
            for entry in batch["entries"] if primed else ():
                state.record_caption(entry["speaker"], " ".join(entry["text"].split()))
                if listener:
                    listener.observe(entry, now)
            primed = True
        if listener:
            listener.poll(state, now)
        session = realtime.get("session")
        if session:
            pump = realtime.get("pcm_pump")
            if not pump or pump.poll() is not None:
                state.set(error="PulseAudio playback stopped. Check paplay and the Google Meet audio patches.",
                          realtime_ready=False)
                return
            if session.audio_bytes_out != state.audio_bytes_out:
                state.set(audio_bytes_out=session.audio_bytes_out, last_audio_out_at=session.last_audio_out_at)
        page.wait_for_timeout(350)


def _connect_browser(playwright, args, url, rt, state, stop):
    saved = read_json(workspace() / "auth.json")
    chromium_args = ["--mute-audio", "--use-fake-ui-for-media-stream"]
    if args.mode == "transcribe":
        chromium_args.append("--use-fake-device-for-media-stream")
    browser = playwright.chromium.launch(
        channel="chromium", executable_path=chromium_executable(), headless=not args.headed,
        args=chromium_args,
    )
    try:
        context = browser.new_context(
            storage_state=saved["storage_state"], locale="en-US", viewport={"width": 1440, "height": 900},
        )
        context.grant_permissions(["microphone"], origin=ORIGIN)
        user = identity(context)
        if user["id"] != saved["user"]["id"]:
            raise ValueError("The saved Soar account changed. Run soar-meet auth as the bot.")
        state.set(signed_in_as=user["name"])
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        state.set(join_attempted_at=time.time())
        connected, started_at = _wait_for_call(page, url, state, stop, until=args.until, start_call=args.start_call)
        if not connected:
            return
        first_participant_deadline = (
            started_at + _FIRST_PARTICIPANT_WAIT_S if args.until is not None and started_at is not None else None
        )
        try:
            _click_if_visible(page, "Turn camera off")
            if args.mode == "realtime":
                _click_if_visible(page, "Unmute microphone")
                _button(page, "Mute microphone").wait_for(state="visible", timeout=10_000)
                state.set(mic_state="unmuted")
                _start_realtime_speaker(rt, _realtime_config(state.out_dir), stop, state)
                if not state.realtime_ready:
                    return
            else:
                _click_if_visible(page, "Mute microphone")
                state.set(mic_state="muted")
            conversation_id = urlsplit(url).path.rsplit("/", 1)[-1]
            _drain(page, state, rt, user["name"] if args.wake else None, stop,
                   conversation_id=conversation_id, wake_session=args.wake_session, until=args.until,
                   first_participant_deadline=first_participant_deadline)
        finally:
            with contextlib.suppress(Exception):
                _click_if_visible(page, "Leave call")
    finally:
        browser.close()


def _realtime_config(out_dir):
    return SimpleNamespace(
        out_dir=out_dir, realtime_api_key=os.environ["HERMES_MEET_REALTIME_KEY"],
        realtime_model=os.environ.get("HERMES_MEET_REALTIME_MODEL") or "gpt-realtime",
        realtime_voice=os.environ.get("HERMES_MEET_REALTIME_VOICE") or "alloy",
        realtime_instructions=os.environ.get("HERMES_MEET_REALTIME_INSTRUCTIONS", ""),
    )


def main():
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("url")
    parser.add_argument("mode", choices=("transcribe", "realtime"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wake", action="store_true")
    parser.add_argument("--wake-session")
    parser.add_argument("--until", type=int)
    parser.add_argument("--start-call", action="store_true")
    args = parser.parse_args()
    url = meeting_url(args.url)
    state = _SoarState(args.out_dir, args.out_dir.name, url)
    stop = {"stop": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.update(stop=True))
    rt = {}
    try:
        if args.mode == "realtime":
            rt["bridge"] = AudioBridge(name_prefix=f"hermes_soar_{os.getpid()}")
            rt["bridge_info"] = rt["bridge"].setup()
            os.environ["PULSE_SOURCE"] = rt["bridge_info"]["device_name"]
            state.set(realtime=True, realtime_device=rt["bridge_info"]["device_name"])
        with sync_playwright() as playwright:
            _connect_browser(playwright, args, url, rt, state, stop)
    except Exception as exc:
        state.set(error=f"{type(exc).__name__}: {exc}")
    finally:
        reason = state.leave_reason or ("requested" if stop["stop"] else "error" if state.error else "call_ended")
        stop["stop"] = True
        _teardown_realtime(rt)
        state.set(in_call=False, captioning=False, realtime_ready=False, exited=True, leave_reason=reason)
    return int(bool(state.error))


if __name__ == "__main__":
    raise SystemExit(main())
