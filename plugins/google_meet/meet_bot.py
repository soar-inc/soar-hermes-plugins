"""Headless Google Meet bot — Playwright + live-caption scraping.

Standalone subprocess spawned by ``process_manager.py``; configured via ``HERMES_MEET_*`` env,
status + transcript written under ``$HERMES_MEET_OUT_DIR`` (filesystem is the only IPC).
No WebRTC audio parsing: Meet's live captions are watched via a MutationObserver — lossy and
English-biased, but deterministic (no STT billing) and stable thanks to the ARIA role.
Debug: ``HERMES_MEET_URL=... HERMES_MEET_OUT_DIR=./meet-out HERMES_MEET_HEADED=1 \\
    python -m plugins.google_meet.meet_bot``
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from hermes_cli.browser_runtime import chromium_executable
from plugins.google_meet.wake_caption import CaptionWake
from utils import atomic_json_write

# Short three-segment code, a lookup URL, or /new. Anything else is rejected.
MEET_URL_RE = re.compile(
    r"^https://meet\.google\.com/([a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,}|lookup/[^/?#]+|new)"
    r"(?:[/?#].*)?$")

_FFMPEG_MISSING = "ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS"


def _is_safe_meet_url(url: str) -> bool:
    """True if *url* is a Google Meet URL we're willing to navigate to."""
    return isinstance(url, str) and bool(MEET_URL_RE.match(url.strip()))


def _meeting_id_from_url(url: str) -> str:
    """3-segment meeting code, or a timestamped id for ``/lookup/...`` and ``/new``."""
    m = re.search(r"meet\.google\.com/([a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,})", url or "")
    return m.group(1) if m else f"meet-{int(time.time())}"


def _quiet(fn, *args, **kwargs):
    """Call *fn*, swallowing any exception (best-effort teardown steps)."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# status.json keys in file order → _BotState attribute + initial value.
_STATUS_FIELDS = (
    ("meetingId", "meeting_id", None), ("url", "url", None), ("inCall", "in_call", False),
    ("captioning", "captioning", False), ("captionsEnabledAttempted", "captions_enabled_attempted", False),
    ("lobbyWaiting", "lobby_waiting", False), ("joinAttemptedAt", "join_attempted_at", None),
    ("joinedAt", "joined_at", None), ("lastCaptionAt", "last_caption_at", None),
    ("transcriptLines", "transcript_lines", 0), ("transcriptPath", "transcript_path", None),
    ("error", "error", None), ("exited", "exited", False), ("pid", None, None),
    # realtime telemetry
    ("realtime", "realtime", False), ("realtimeReady", "realtime_ready", False),
    ("realtimeDevice", "realtime_device", None), ("audioBytesOut", "audio_bytes_out", 0),
    ("lastAudioOutAt", "last_audio_out_at", None), ("lastBargeInAt", "last_barge_in_at", None),
    ("wakeEnabled", "wake_enabled", False), ("wakeQuestions", "wake_questions", 0),
    ("wakeError", "wake_error", None), ("wakeSessionId", "wake_session_id", None),
    ("presentationState", "presentation_state", None), ("presentationTab", "presentation_tab", None),
    ("presentationError", "presentation_error", None),
    ("leaveReason", "leave_reason", None), ("micState", "mic_state", None))


class _BotState:
    """Single-process mutable state, flushed to ``status.json`` on each change."""

    def __init__(self, out_dir: Path, meeting_id: str, url: str):
        self.__dict__.update({attr: default for _, attr, default in _STATUS_FIELDS if attr})
        self.__dict__.update(out_dir=out_dir, meeting_id=meeting_id, url=url, _seen=set(),  # seen "speaker|text"
                             transcript_path=out_dir / "transcript.txt", status_path=out_dir / "status.json")
        out_dir.mkdir(parents=True, exist_ok=True)
        self._flush()

    def record_caption(self, speaker: str, text: str) -> None:
        """Append a caption line unless this exact (speaker, text) was already seen."""
        speaker, text = (speaker or "").strip() or "Unknown", (text or "").strip()
        key = f"{speaker}|{text}"
        if not text or key in self._seen:
            return
        self._seen.add(key)
        self.transcript_lines += 1
        self.last_caption_at = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(self.last_caption_at))
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {speaker}: {text}\n")
        self._flush()

    def _flush(self) -> None:
        data = {key: getattr(self, attr) if attr else None for key, attr, _ in _STATUS_FIELDS}
        data.update(transcriptPath=str(self.transcript_path), pid=os.getpid())  # keeps table key order
        atomic_json_write(self.status_path, data)

    def set(self, **kwargs) -> None:
        self.__dict__.update(kwargs)
        self._flush()


# JS injected into the Meet tab: MutationObserver on the caption container
# collects {speaker, text}; ``window.__hermesMeetDrain()`` pulls new entries.
_CAPTION_OBSERVER_JS = r"""
(() => {
  if (typeof window.__hermesMeetDrain === 'function') return;
  window.__hermesMeetQueue = [];
  const rowsSeen = new WeakMap();
  let nextRow = 0;

  const captionSelector = '[role="region"][aria-label*="aption" i], ' +
                          'div[jsname="YSxPC"], ' +  // legacy
                          'div[jsname="tgaKEf"]';    // current (Apr 2026)

  function pushEntry(element, speaker, text) {
    speaker = (speaker || '').trim();
    text = (text || '').trim();
    const previous = rowsSeen.get(element);
    if (previous && previous.speaker === speaker && previous.text === text) return;
    const sameSpeaker = previous && previous.speaker === speaker;
    const row = sameSpeaker ? previous.row : ++nextRow;
    rowsSeen.set(element, { row, speaker, text });
    if (!text) return;
    window.__hermesMeetQueue.push({
      ts: Date.now(), row, speaker, text, previous: sameSpeaker ? previous.text : '',
    });
  }

  function visible(element) {
    if (!element.getClientRects().length) return false;
    if (typeof element.checkVisibility === 'function') {
      return element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
    }
    const style = getComputedStyle(element);
    return style.visibility !== 'hidden' && style.visibility !== 'collapse';
  }

  function scan(root) {
    // Meet captions render as rows of speaker label + text block. Selectors
    // vary across Meet rewrites; try a few shapes and fall back to raw text.
    const rows = root.querySelectorAll('div[jsname="dsyhDe"], div.CNusmb, div.TBMuR');
    if (rows.length) {
      rows.forEach((row) => {
        if (!visible(row)) return;
        const spkEl = row.querySelector('div.KcIKyf, div.zs7s8d, span[jsname="YSxPC"]');
        const txtEl = row.querySelector('div.bh44bd, span[jsname="tgaKEf"], div.iTTPOb');
        pushEntry(row, spkEl ? spkEl.innerText : '', txtEl ? txtEl.innerText : row.innerText);
      });
      return;
    }
    // Fallback: treat the whole region's innerText as one anonymous line.
    pushEntry(root, '', (root.innerText || '').split('\n').filter(Boolean).pop());
  }

  let attached = [];
  const options = { childList: true, subtree: true, characterData: true };
  const scanVisible = () => attached.filter(visible).forEach(scan);
  const observer = new MutationObserver(scanVisible);
  function attach() {
    // Meet can leave a hidden copy mounted when switching between call layouts.
    // Observe each visible region once, even if a legacy selector matches a child.
    const candidates = Array.from(document.querySelectorAll(captionSelector)).filter(visible);
    const regions = candidates.filter(region =>
      !candidates.some(other => other !== region && other.contains(region)));
    if (regions.length !== attached.length || regions.some((region, i) => region !== attached[i])) {
      observer.disconnect();
      attached = regions;
      attached.forEach(region => observer.observe(region, options));
    }
    // Visibility can change without a text mutation, including on an existing row.
    scanVisible();
  }
  attach();
  setInterval(attach, 500);

  window.__hermesMeetDrain = () => {
    const out = window.__hermesMeetQueue.slice();
    window.__hermesMeetQueue = [];
    return out;
  };
})();
"""

_CAPTION_DRAIN_JS = "window.__hermesMeetDrain && window.__hermesMeetDrain()"
_CAPTION_CHECK_INTERVAL = 5.0
_ENABLE_CAPTIONS = re.compile(r"^(?:(?:turn on|show) captions|turn captions on)(?:\s*\([^)]+\))?$", re.I)
_DISABLE_CAPTIONS = re.compile(r"^(?:(?:turn off|hide) captions|turn captions off)(?:\s*\([^)]+\))?$", re.I)

_LEAVE_CALL_JS = (
    "() => { const b = document.querySelector('button[aria-label*=\"eave call\"]');"
    " if (b) b.click(); }")

# Require visible controls from the in-call toolbar. The lobby can also
# contain text or accessible labels mentioning participants.
_ADMISSION_PROBE_JS = r"""
    (() => {
      const bodyText = document.body?.innerText || '';
      const visible = (node) => node.getClientRects().length > 0
        && getComputedStyle(node).visibility === 'visible'
        && !node.closest('[aria-hidden="true"]');
      const controls = [...document.querySelectorAll(
        'button[aria-label], [role="button"][aria-label]'
      )].filter(visible).map((node) => node.getAttribute('aria-label').trim());
      const has = (pattern) => controls.some((label) => pattern.test(label));
      if (/You can['’]t join this video call|You were removed from the meeting|No one responded to your request to join|Your request to join was denied/i.test(bodyText)) {
        return 'denied';
      }
      if (/asking to join|you(?:'|’)ve asked to join|waiting to be admitted|waiting for (?:the )?host to let you in|someone will let you in soon|you(?:'|’)ll join when someone lets you in/i.test(bodyText)
          || has(/^cancel (?:join )?request$/i)) {
        return 'lobby';
      }
      if (has(/^(?:join now|ask to join)$/i)) return 'pending';

      const leave = has(/^leave call(?:\s*\([^)]+\))?$/i);
      const inCallControl = has(/^(?:show everyone|(?:turn (?:on|off)|show|hide) captions|turn captions (?:on|off)|present now|chat with everyone)(?:\s*\([^)]+\))?$/i);
      return leave && inCallControl ? 'admitted' : 'pending';
    })();
    """

# Read the People control's total rather than counting visible video tiles.
_PARTICIPANT_COUNT_JS = r"""
    () => {
      const visible = (node) => node.getClientRects().length > 0
        && getComputedStyle(node).visibility === "visible";
      if (![...document.querySelectorAll('button[aria-label*="eave call" i]')].some(visible)) {
        return null;
      }
      const counts = new Set();
      for (const button of document.querySelectorAll('button[aria-label], [role="button"][aria-label]')) {
        const label = button.getAttribute("aria-label").trim();
        const match = /^(?:Show everyone|People|Participants)(?:\s*\(\s*(\d+)\s*\))?$/i.exec(label);
        if (!match || !visible(button)) continue;
        const text = button.innerText.trim();
        const badge = /^(?:(?:people|group|groups|person)\s+)?(\d+)$/i.exec(text);
        if (match[1]) counts.add(Number(match[1]));
        if (badge) counts.add(Number(badge[1]));
      }
      return counts.size === 1 ? [...counts][0] : null;
    }
    """


def _visible(locator):
    """``locator.first`` if it exists and is visible, else None (swallows Playwright errors)."""
    return _quiet(lambda: locator.first if locator.first.count() and locator.first.is_visible() else None)


def _pcm_tail_loop(proc, pcm_path: Path, stop_flag: dict, poll_interval: float = 0.05) -> None:
    """Follow ``speaker.pcm`` as it grows and forward every appended chunk to the pump's stdin.
    The pump itself would hit EOF on the (empty) file at start-up and exit; this thread keeps
    feeding it until the stop flag is set or the pump dies."""
    try:
        with open(pcm_path, "rb") as f:
            while not stop_flag.get("stop") and proc.poll() is None:
                chunk = f.read(65536)
                if not chunk:
                    time.sleep(poll_interval)
                    continue
                proc.stdin.write(chunk)
                proc.stdin.flush()
    except (OSError, ValueError):
        pass  # pump exited / pipe closed (BrokenPipeError, write on closed stdin): nothing left to stream to
    finally:
        _quiet(proc.stdin.close)


def _start_pcm_pump(rt: dict, bridge_info: dict, pcm_path: Path, state: _BotState,
                    stop_flag: dict) -> None:
    """Stream the growing ``speaker.pcm`` (24kHz s16le mono) into the device Chrome's fake mic reads.
    The pump reads raw PCM from stdin so audio appended after start-up is still played —
    pointed at the file it would read the empty sink to EOF and exit before Realtime spoke."""
    bridge_info = bridge_info or {}
    platform_tag = bridge_info.get("platform")
    target = bridge_info.get("write_target")
    if platform_tag == "linux":
        cmd = ["paplay", "--raw", "--rate=24000", "--format=s16le", "--channels=1",
               f"--device={target or 'hermes_meet_sink'}"]
        missing = "paplay not found — install pulseaudio-utils for realtime on Linux"
    elif platform_tag == "darwin":
        # User must have BlackHole as default input; ffmpeg targets it by audiotoolbox index.
        if not shutil.which("ffmpeg"):
            state.set(error=_FFMPEG_MISSING)
            return
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "-", "-f", "audiotoolbox",
               "-audio_device_index", _mac_audio_device_index(target or "BlackHole 2ch"), "-"]
        missing = _FFMPEG_MISSING
    else:
        return
    try:
        rt["pcm_pump"] = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        state.set(error=missing)
        return
    except Exception as e:
        if platform_tag != "darwin":
            raise
        state.set(error=f"macOS pcm pump failed to start: {e}")
        return
    rt["pcm_tail_thread"] = threading.Thread(
        target=_pcm_tail_loop, args=(rt["pcm_pump"], pcm_path, stop_flag), name="meet-pcm-tail", daemon=True)
    rt["pcm_tail_thread"].start()


def _start_realtime_speaker(rt: dict, cfg: _BotConfig, stop_flag: dict, state: _BotState) -> None:
    """Wire up the OpenAI Realtime session, the say-queue speaker thread and the PCM pump."""
    pcm_path, queue_path = cfg.out_dir / "speaker.pcm", cfg.out_dir / "say_queue.jsonl"
    pcm_path.write_bytes(b"")  # clean sink file per session
    queue_path.touch()  # so the speaker poller doesn't error on first iteration
    phase = "import"
    try:
        from plugins.google_meet.realtime.openai_client import RealtimeSession, RealtimeSpeaker
        phase = "connect"
        session = RealtimeSession(
            api_key=cfg.realtime_api_key, model=cfg.realtime_model, voice=cfg.realtime_voice,
            instructions=cfg.realtime_instructions, audio_sink_path=pcm_path, sample_rate=24000)
        session.connect()
    except Exception as e:
        state.set(error=f"realtime {phase} failed: {e}")
        return
    rt["session"] = session
    speaker = RealtimeSpeaker(session=session, queue_path=queue_path,
                              processed_path=cfg.out_dir / "say_processed.jsonl")

    def _speaker_loop():
        try:
            speaker.run_until_stopped(lambda: stop_flag.get("stop", False))
        except Exception as e:
            state.set(error=f"realtime speaker crashed: {e}")

    rt["speaker_thread"] = threading.Thread(target=_speaker_loop, name="meet-speaker", daemon=True)
    rt["speaker_thread"].start()
    _start_pcm_pump(rt, rt["bridge_info"], pcm_path, state, stop_flag)
    state.set(realtime_ready=True)


def _mac_audio_device_index(device_name: str) -> str:
    """ffmpeg ``-audio_device_index`` for *device_name* (case-insensitive; ``"0"`` if not found).
    ffmpeg prints the avfoundation device table on stderr as ``[N] Name``."""
    out = _quiet(subprocess.run, ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                 capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    needle = device_name.strip().lower()
    for line in (out.stderr if out else "").splitlines():
        m = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if m and m.group(2).strip().lower() == needle:
            return m.group(1)
    return "0"


def _setup_realtime(rt: dict, api_key: str, state: _BotState) -> None:
    """Provision the virtual audio bridge; on any failure fall back to transcribe mode."""
    if not api_key:
        state.set(error="realtime mode requested but no API key in HERMES_MEET_REALTIME_KEY/OPENAI_API_KEY — falling back to transcribe")
        rt["enabled"] = False
        return
    try:
        from plugins.google_meet.audio_bridge import AudioBridge
        rt["bridge"] = AudioBridge()
        rt["bridge_info"] = rt["bridge"].setup()
        state.set(realtime=True, realtime_device=rt["bridge_info"].get("device_name"))
    except Exception as e:
        state.set(error=f"audio bridge setup failed: {e} — falling back to transcribe")
        rt["enabled"] = False


def _teardown_realtime(rt: dict) -> None:
    if rt.get("pcm_pump"):
        _quiet(rt["pcm_pump"].terminate)
        _quiet(rt["pcm_pump"].wait, timeout=3)
    for key, method, kw in (("pcm_tail_thread", "join", {"timeout": 1.0}),
                            ("speaker_thread", "join", {"timeout": 5.0}), ("session", "close", {}),
                            ("bridge", "teardown", {})):
        if rt.get(key) is not None:
            _quiet(getattr(rt[key], method), **kw)


_BotConfig = SimpleNamespace  # everything the bot reads from ``HERMES_MEET_*`` env vars


def _config_from_env() -> _BotConfig:
    env = os.environ.get
    out_raw = env("HERMES_MEET_OUT_DIR", "").strip()
    return _BotConfig(
        url=env("HERMES_MEET_URL", "").strip(),
        out_dir=Path(out_raw) if out_raw else None,
        headed=env("HERMES_MEET_HEADED", "").lower() in {"1", "true", "yes"},
        auth_state=env("HERMES_MEET_AUTH_STATE", "").strip(),
        voice_share=env("HERMES_MEET_VOICE_SHARE") == "1",
        guest_name=env("HERMES_MEET_GUEST_NAME", "Hermes Agent"),
        duration_s=_parse_duration(env("HERMES_MEET_DURATION", "")),
        realtime=env("HERMES_MEET_MODE", "transcribe").strip().lower() == "realtime",
        # HERMES_MEET_REALTIME_KEY is resolved by process_manager.start() via the parent's
        # profile secret scope; OPENAI_API_KEY only serves standalone `python -m` runs.
        realtime_api_key=env("HERMES_MEET_REALTIME_KEY") or env("OPENAI_API_KEY", ""),
        realtime_model=env("HERMES_MEET_REALTIME_MODEL", "gpt-realtime"),
        realtime_voice=env("HERMES_MEET_REALTIME_VOICE", "alloy"),
        realtime_instructions=env("HERMES_MEET_REALTIME_INSTRUCTIONS", ""),
        lobby_timeout=float(env("HERMES_MEET_LOBBY_TIMEOUT", "300")))


def _join(page, cfg: _BotConfig, state: _BotState, timeout: float = 30.0) -> None:
    """Fill the guest-name field and click 'Join now' / 'Ask to join' (the latter → lobby_waiting).
    Meet renders the pre-join buttons asynchronously after ``domcontentloaded``, so poll for up to
    *timeout* seconds instead of checking once — a single miss leaves the bot silently in the lobby."""
    deadline = time.time() + timeout
    while True:
        name_box = _visible(page.locator('input[aria-label*="name" i]'))
        if name_box is not None:
            _quiet(name_box.fill, cfg.guest_name, timeout=2_000)
        for label in ("Join now", "Ask to join"):
            btn = _visible(page.get_by_role("button", name=label, exact=False))
            if btn is not None and _quiet(lambda: (btn.click(timeout=3_000), True)):
                if label == "Ask to join":
                    state.set(lobby_waiting=True)
                return
        if time.time() >= deadline:
            return
        time.sleep(0.5)


def _ensure_mic_on(page) -> str:
    """Unmute the bot once admitted — Meet may seat an authenticated bot muted, and a muted mic makes
    realtime speech inaudible. The in-call toggle's aria-label is "Turn on microphone" while muted /
    "Turn off microphone" while live; returns ``unmuted_clicked`` / ``unmuted`` / ``unknown``
    (toggle not found: Meet variant changed or the label is localized)."""
    muted = _visible(page.locator('button[aria-label*="Turn on microphone" i]'))
    if muted is not None and _quiet(lambda: (muted.click(timeout=3_000), True)):
        return "unmuted_clicked"
    if _visible(page.locator('button[aria-label*="Turn off microphone" i]')) is not None:
        return "unmuted"
    return "unknown"


def _ensure_captions(page, state: _BotState, *, allow_shortcut: bool = False) -> None:
    """Enable an explicitly disabled caption control without toggling an enabled one."""
    if _visible(page.get_by_role("button", name=_DISABLE_CAPTIONS)):
        if not state.captioning:
            state.set(captioning=True)
        return
    toggle = _visible(page.get_by_role("button", name=_ENABLE_CAPTIONS))
    if toggle is not None:
        if state.captioning:
            state.set(captioning=False)
        toggle.click(timeout=3_000)
    elif allow_shortcut:
        page.keyboard.press("c")
    else:
        return
    if not state.captions_enabled_attempted:
        state.set(captions_enabled_attempted=True)


def _start_captions(page, state: _BotState, *, allow_shortcut: bool = True) -> None:
    """Install the observer in the current document and enable its captions."""
    try:
        page.evaluate(_CAPTION_OBSERVER_JS)
        _ensure_captions(page, state, allow_shortcut=allow_shortcut)
    except Exception as exc:
        state.set(error=f"caption setup failed: {exc}")


def _drain_loop(page, cfg: _BotConfig, state: _BotState, rt: dict, stop_flag: dict) -> None:
    """Admission + caption drain loop until SIGTERM, duration expiry, lobby timeout/denial or page loss.
    Sets ``leave_reason`` for every exit but SIGTERM; triggers barge-in; mirrors realtime counters."""
    deadline = (time.time() + cfg.duration_s) if cfg.duration_s else None
    lobby_deadline = time.time() + cfg.lobby_timeout
    last_admission_check = 0.0
    next_participant_check = 0.0
    next_caption_check = 0.0
    had_company = False
    alone_since = None
    wake = rt.get("wake")
    while not stop_flag["stop"]:
        now = time.time()
        if deadline and now > deadline:
            state.set(leave_reason="duration_expired")
            return
        if not state.in_call and (now - last_admission_check) > 3.0:
            last_admission_check = now
            admission = _quiet(page.evaluate, _ADMISSION_PROBE_JS)
            if admission == "denied":
                state.set(lobby_waiting=False, error="host denied admission", leave_reason="denied")
                return
            if admission == "admitted":
                state.set(in_call=True, lobby_waiting=False, joined_at=now, mic_state=_ensure_mic_on(page))
                _start_captions(page, state)
                next_caption_check = time.monotonic() + _CAPTION_CHECK_INTERVAL
            elif now > lobby_deadline:
                waited = int(lobby_deadline - state.join_attempted_at) if state.join_attempted_at else 0
                state.set(lobby_waiting=False,
                          error=f"lobby timeout — host never admitted the bot within {waited}s",
                          leave_reason="lobby_timeout")
                return
            elif admission == "lobby" and not state.lobby_waiting:
                state.set(lobby_waiting=True)
        if state.in_call and time.monotonic() >= next_participant_check:
            participants = _quiet(page.evaluate, _PARTICIPANT_COUNT_JS)
            checked_at = time.monotonic()
            next_participant_check = checked_at + 5
            if type(participants) is int and participants > 1:
                had_company = True
            if had_company and type(participants) is int and participants == 1:
                if alone_since is None:
                    alone_since = checked_at
                elif checked_at - alone_since >= 15:
                    state.set(leave_reason="all_participants_left")
                    return
            else:
                alone_since = None
        try:
            queued = page.evaluate(_CAPTION_DRAIN_JS)
            if state.in_call and time.monotonic() >= next_caption_check:
                next_caption_check = time.monotonic() + _CAPTION_CHECK_INTERVAL
                if queued is None:
                    # A new document has no drain function. Wait for the in-call toolbar
                    # before reinstalling so a reload into the lobby cannot trigger input.
                    if state.captioning:
                        state.set(captioning=False)
                    if _quiet(page.evaluate, _ADMISSION_PROBE_JS) == "admitted":
                        _start_captions(page, state, allow_shortcut=False)
                else:
                    _ensure_captions(page, state)
            for entry in (e for e in (queued if isinstance(queued, list) else ()) if isinstance(e, dict)):
                speaker = str(entry.get("speaker", ""))
                text = str(entry.get("text", ""))
                if state.in_call and text.strip() and not state.captioning:
                    state.set(captioning=True)
                state.record_caption(speaker=speaker, text=text)
                if wake is not None and state.in_call:
                    wake.observe(entry, time.monotonic())
                # Barge-in: a real human spoke while we may be generating audio.
                if (rt["session"] is not None and _looks_like_human_speaker(speaker, cfg.guest_name)
                        and _quiet(rt["session"].cancel_response)):
                    state.set(last_barge_in_at=now)
        except Exception:
            if page.is_closed():  # Meet reloaded or we got booted — exit rather than spin
                state.set(leave_reason="page_closed")
                return
        if (presentation := rt.get("presentation")) is not None and state.in_call:
            _quiet(presentation.poll)
        if wake is not None and state.in_call and wake.poll(state, time.monotonic()):
            stop_flag["stop"] = True
            state.set(leave_reason="voice_requested")
            return
        if rt["session"] is not None:
            state.set(audio_bytes_out=rt["session"].audio_bytes_out,
                      last_audio_out_at=rt["session"].last_audio_out_at)
        time.sleep(1.0)


_CONTEXT_ARGS = {
    "viewport": {"width": 1280, "height": 800},
    "user_agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "permissions": ["microphone", "camera"]}


def run_bot() -> int:
    cfg = _config_from_env()
    if not _is_safe_meet_url(cfg.url):
        sys.stderr.write("google_meet bot: refusing to launch — HERMES_MEET_URL must be a "
                         f"meet.google.com URL. got: {cfg.url!r}\n")
        return 2
    if cfg.out_dir is None:
        sys.stderr.write("google_meet bot: HERMES_MEET_OUT_DIR is required\n")
        return 2
    state = _BotState(out_dir=cfg.out_dir, meeting_id=_meeting_id_from_url(cfg.url), url=cfg.url)
    # SIGTERM sets a flag (not an exception) so the Playwright teardown below still runs
    # and ``meet_leave`` gets a finalized transcript.
    stop_flag = {"stop": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stop_flag.__setitem__("stop", True))
    # Realtime resources in one dict so teardown works however we exit.
    rt = dict(enabled=cfg.realtime, bridge=None, bridge_info=None, session=None, speaker_thread=None)
    if rt["enabled"]:
        _setup_realtime(rt, cfg.realtime_api_key, state)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        state.set(error=f"playwright not installed: {e}", exited=True)
        sys.stderr.write("google_meet bot: playwright is not installed. Run "
                         "`hermes meet install`\n")
        if rt["bridge"]:
            rt["bridge"].teardown()
        return 3
    from plugins.google_meet.chrome_profile import meet_chrome_profile, persistent_chrome_enabled

    use_persistent_chrome = cfg.voice_share or persistent_chrome_enabled()
    chrome_args = ["--use-fake-ui-for-media-stream", "--disable-blink-features=AutomationControlled"]
    if cfg.voice_share:
        chrome_args.remove("--use-fake-ui-for-media-stream")
        from plugins.google_meet.presentation import capture_browser_config

        capture_key, capture_guard = capture_browser_config(cfg.url)
        chrome_args.append(f"--auto-select-tab-capture-source-by-title={capture_key}")
    if not rt["enabled"]:
        chrome_args.insert(1, "--use-fake-device-for-media-stream")  # silent fake mic
    elif rt["bridge_info"] and rt["bridge_info"].get("platform") == "linux":
        # Playwright's launch() takes no env: set PULSE_SOURCE on ourselves so Chrome inherits it.
        os.environ["PULSE_SOURCE"] = rt["bridge_info"].get("device_name", "")
    context_args = dict(_CONTEXT_ARGS)
    if use_persistent_chrome:
        context_args.pop("permissions")
        context_args.pop("user_agent")  # Keep the same browser identity used by the sign-in helper.
    if cfg.voice_share:
        context_args["service_workers"] = "block"
    auth_state = cfg.auth_state if cfg.auth_state and Path(cfg.auth_state).is_file() else None
    try:
        with sync_playwright() as pw, ExitStack() as stack:
            launch_args = dict(channel="chromium", executable_path=chromium_executable(),
                               headless=not cfg.headed, args=chrome_args)
            if use_persistent_chrome:
                from plugins.google_meet.presentation import seed_legacy_flowmind

                profile = stack.enter_context(meet_chrome_profile())
                if cfg.voice_share:
                    launch_args["ignore_default_args"] = ["--disable-infobars"]
                context = pw.chromium.launch_persistent_context(profile, **launch_args, **context_args)
                stack.callback(_quiet, context.close)
                seed_legacy_flowmind(context, profile)
                context.grant_permissions(["microphone", "camera"], origin="https://meet.google.com")
                if cfg.voice_share:
                    context.add_init_script(capture_guard)
                page = context.pages[0]
            else:
                browser = pw.chromium.launch(**launch_args)
                stack.callback(_quiet, browser.close)
                if auth_state:
                    context_args["storage_state"] = auth_state
                context = browser.new_context(**context_args)
                stack.callback(_quiet, context.close)
                page = context.new_page()
            try:
                page.goto(cfg.url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                state.set(error=f"navigate failed: {e}", exited=True)
                return 4
            _join(page, cfg, state)
            state.set(join_attempted_at=time.time())
            if rt["enabled"]:
                _start_realtime_speaker(rt, cfg, stop_flag, state)
                if state.realtime_ready and os.environ.get("HERMES_MEET_WAKE") == "1":
                    wake_session = os.environ.get("HERMES_MEET_WAKE_SESSION") or None
                    on_prompt = None
                    if cfg.voice_share:
                        from plugins.google_meet.agent_turn import MeetAgentTurn
                        from plugins.google_meet.presentation import Presentation

                        presentation = Presentation(page, state, capture_key=capture_key)
                        rt["presentation"] = presentation
                        on_prompt = MeetAgentTurn(presentation, wake_session)
                        state.set(presentation_state="idle")
                    rt["wake"] = CaptionWake(
                        cfg.guest_name, cfg.out_dir, wake_session=wake_session, on_prompt=on_prompt,
                    )
                    state.set(wake_enabled=True, wake_session_id=wake_session)
            try:
                _drain_loop(page, cfg, state, rt, stop_flag)
            finally:
                if (presentation := rt.get("presentation")) is not None:
                    presentation.close()
            _quiet(page.evaluate, _LEAVE_CALL_JS)
            stack.close()
            _teardown_realtime(rt)
            state.set(in_call=False, lobby_waiting=False, captioning=False, exited=True)
            return 0
    except Exception as e:
        state.set(error=f"unhandled: {e}", exited=True)
        return 1


def _looks_like_human_speaker(speaker: str, bot_guest_name: str) -> bool:
    """Whether a caption's speaker is probably a human rather than our own echo (Meet attributes
    our fake-mic audio to the bot's name; blank/unknown speakers are ambiguous — no barge-in)."""
    return bool(speaker and speaker.strip()) and (
        speaker.strip().lower() not in {"unknown", "you", bot_guest_name.strip().lower()})


_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0}


def _parse_duration(raw: str) -> Optional[float]:
    """Parse ``30m`` / ``2h`` / ``90`` (seconds) → float seconds, or None."""
    if not raw:
        return None
    raw = raw.strip().lower()
    mult = _DURATION_UNITS.get(raw[-1:])
    try:
        return float(raw[:-1]) * mult if mult else float(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover — subprocess entry point
    sys.exit(run_bot())
