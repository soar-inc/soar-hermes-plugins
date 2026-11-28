"""OpenAI Realtime API WebSocket client + file-queue speaker.

text → OpenAI Realtime → audio deltas appended as PCM to a file the audio bridge streams into
Chrome's fake mic. One sync WebSocket per session; ``websockets`` is imported lazily.
"""

from __future__ import annotations

import base64
import contextlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from plugins.google_meet._jsonfile import say_queue_lock


REALTIME_URL = "wss://api.openai.com/v1/realtime"

_TERMINAL_FRAMES = {"response.done", "response.completed", "response.cancelled"}


def _decode_audio(b64: str) -> bytes:
    try:
        return base64.b64decode(b64) if b64 else b""
    except (ValueError, TypeError):
        return b""


class RealtimeSession:
    """Minimal sync client for the OpenAI Realtime WebSocket API; ``speak`` and ``cancel_response``
    may run on different threads — a lock serializes WebSocket writes."""

    def __init__(self, api_key: str, model: str = "gpt-realtime", voice: str = "alloy",
                 instructions: str = "", audio_sink_path: Optional[Path] = None, sample_rate: int = 24000) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.audio_sink_path = Path(audio_sink_path) if audio_sink_path else None
        self.sample_rate = sample_rate
        self._ws: Any = None
        self._send_lock = threading.Lock()
        self._active_response_id: Optional[str] = None
        self._cancel_sent = False
        self._closed = False
        self.audio_bytes_out: int = 0  # public counters for status reporting
        self.last_audio_out_at: Optional[float] = None

    def connect(self) -> None:
        """Open the WebSocket and wait for OpenAI to accept the audio settings."""
        try:
            from websockets.sync.client import connect  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised via test
            raise RuntimeError("websockets package is required for OpenAI Realtime; "
                               "install with: pip install websockets") from exc
        url = f"{REALTIME_URL}?model={self.model}"
        headers = [("Authorization", f"Bearer {self.api_key}")]
        # Allow brief network stalls while still detecting dead sockets.
        options = {"ping_interval": 20, "ping_timeout": 45, "close_timeout": 3}
        try:
            ws = connect(url, additional_headers=headers, **options)
        except TypeError:
            ws = connect(url, extra_headers=headers)
        with self._send_lock:
            closed = self._closed
            if not closed:
                self._ws = ws
        if closed:
            with contextlib.suppress(Exception):
                ws.close()
            raise RuntimeError("Realtime session is closed")
        try:
            self._send_json({"type": "session.update", "session": {
                "type": "realtime",
                "instructions": self.instructions,
                "output_modalities": ["audio"],
                "audio": {"output": {
                    "voice": self.voice,
                    "format": {"type": "audio/pcm", "rate": self.sample_rate},
                }},
            }})
            timeout = 10.0
            deadline = time.monotonic() + timeout
            while True:
                frame = self._recv_frame(deadline, timeout)
                if frame is None:
                    raise RuntimeError("realtime connection closed before session settings were accepted")
                if frame.get("type") == "session.updated":
                    return
                if frame.get("type") == "error":
                    raise RuntimeError(f"realtime session update failed: {frame.get('error') or frame}")
        except Exception:
            self._disconnect()
            raise

    def _disconnect(self) -> None:
        with self._send_lock:
            ws, self._ws = self._ws, None
            self._active_response_id = None
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()

    def close(self) -> None:
        with self._send_lock:
            self._closed = True
        self._disconnect()

    def speak(self, text: str, timeout: float = 30.0, *, timings: dict[str, int | None] | None = None) -> dict:
        """Reconnect and retry once if transport fails before any reply audio is written."""
        from websockets.exceptions import ConnectionClosed

        for attempt in range(2):
            with self._send_lock:
                if self._closed:
                    raise RuntimeError("Realtime session is closed")
                self._active_response_id = None
                self._cancel_sent = False
            before = self.audio_bytes_out
            try:
                if self._ws is None:
                    self.connect()
                return self._speak_once(text, timeout, timings)
            except (ConnectionClosed, OSError):
                with self._send_lock:
                    cancelled, closed = self._cancel_sent, self._closed
                self._disconnect()
                # Do not repeat a reply the attendee interrupted or partly heard.
                if attempt or cancelled or closed or self.audio_bytes_out != before:
                    raise
            except Exception:
                self._disconnect()
                raise
            finally:
                with self._send_lock:
                    self._active_response_id = None
                    self._cancel_sent = False

    def _speak_once(self, text: str, timeout: float, timings: dict[str, int | None] | None) -> dict:
        """Send text and stream the reply to the file consumed by the audio bridge."""
        start = time.monotonic()
        self._send_json({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        self._send_json({"type": "response.create", "response": {"output_modalities": ["audio"]}})
        if timings is not None:
            timings.update(
                realtime_sent_at_ns=time.time_ns(),
                response_created_at_ns=None,
                first_audio_frame_received_at_ns=None,
            )
        bytes_written = 0
        with contextlib.ExitStack() as stack:
            sink_fp = None
            if self.audio_sink_path is not None:
                self.audio_sink_path.parent.mkdir(parents=True, exist_ok=True)
                sink_fp = stack.enter_context(open(self.audio_sink_path, "ab"))
            while True:
                frame = self._recv_frame(start + timeout, timeout, timings=timings)
                if frame is None:
                    raise ConnectionError("Realtime connection closed before the response completed")
                ftype = frame.get("type")
                if ftype == "response.created":
                    response_id = (frame.get("response") or {}).get("id")
                    if isinstance(response_id, str) and response_id:
                        with self._send_lock:
                            self._active_response_id = response_id
                    continue
                if ftype in _TERMINAL_FRAMES:
                    response = frame.get("response") or {}
                    with self._send_lock:
                        self._active_response_id = None
                    if response.get("status") in {"failed", "incomplete"}:
                        raise RuntimeError(f"Realtime response {response['status']}")
                    break
                if ftype == "error":
                    error = frame.get("error") or {}
                    if error.get("code") == "response_cancel_not_active":
                        # The server can finish just before an in-flight cancel arrives.
                        continue
                    raise RuntimeError(f"realtime error: {error or frame}")
                chunk = _decode_audio(frame.get("delta") or frame.get("audio") or "") if (
                    ftype == "response.output_audio.delta" and sink_fp is not None) else b""
                if chunk:
                    sink_fp.write(chunk)
                    sink_fp.flush()
                    if timings is not None and bytes_written == 0:
                        timings["first_pcm_at_ns"] = time.time_ns()
                    bytes_written += len(chunk)
                    self.audio_bytes_out += len(chunk)
                    self.last_audio_out_at = time.time()
        return {"ok": True, "bytes_written": bytes_written, "duration_ms": (time.monotonic() - start) * 1000.0}

    def cancel_response(self) -> bool:
        """Barge-in: send ``response.cancel``. True if sent, False if nothing to cancel / socket closed."""
        with self._send_lock:
            ws = self._ws
            response_id = self._active_response_id
            if self._closed or ws is None or response_id is None or self._cancel_sent:
                return False
            try:
                ws.send(json.dumps({"type": "response.cancel", "response_id": response_id}))
            except Exception:
                return False
            self._cancel_sent = True
            return True

    def _send_json(self, payload: dict) -> None:
        with self._send_lock:
            if self._ws is None:
                raise ConnectionError("Realtime connection is closed")
            self._ws.send(json.dumps(payload))

    def _recv_frame(self, deadline: float, timeout: float, *,
                    timings: dict[str, int | None] | None = None) -> Optional[dict]:
        """Next dict frame before *deadline* (monotonic), ``None`` once the peer closes.
        Non-dict / unparseable frames are skipped; TimeoutError past the deadline."""
        ws = self._ws
        if ws is None:
            raise ConnectionError("Realtime connection is closed")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"realtime response did not complete within {timeout}s")
            try:
                raw = ws.recv(timeout=remaining)
            except TypeError:  # older websockets: no timeout kwarg
                raw = ws.recv()
            if raw is None:
                return None
            received_at_ns = None
            if timings is not None and (
                timings["response_created_at_ns"] is None or timings["first_audio_frame_received_at_ns"] is None
            ):
                received_at_ns = time.time_ns()
            with contextlib.suppress(TypeError, ValueError):
                frame = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
                if isinstance(frame, dict):
                    if timings is not None and received_at_ns is not None:
                        event = frame.get("type")
                        if event == "response.created" and timings["response_created_at_ns"] is None:
                            timings["response_created_at_ns"] = received_at_ns
                        elif (event == "response.output_audio.delta" and (frame.get("delta") or frame.get("audio"))
                              and timings["first_audio_frame_received_at_ns"] is None):
                            timings["first_audio_frame_received_at_ns"] = received_at_ns
                    return frame


class RealtimeSpeaker:
    """JSONL queue (``{"id", "text"}`` per line) wrapper around :class:`RealtimeSession`; processed
    lines are appended to ``processed_path`` (if set) and removed from the queue."""

    def __init__(self, session: RealtimeSession, queue_path: Path, processed_path: Optional[Path] = None) -> None:
        self.session = session
        self.queue_path = Path(queue_path)
        self.processed_path = Path(processed_path) if processed_path else None

    def _read_queue(self) -> list[dict]:
        """Parse the JSONL queue, skipping blank/malformed lines; entries lacking an ``id`` get one."""
        if not self.queue_path.exists():
            return []
        out: list[dict] = []
        for line in self.queue_path.read_text(encoding="utf-8-sig").splitlines():
            with contextlib.suppress(ValueError):
                entry = json.loads(line) if line.strip() else None
                if isinstance(entry, dict):
                    entry.setdefault("id", str(uuid.uuid4()))
                    out.append(entry)
        return out

    def _rewrite_queue(self, remaining: list[dict]) -> None:
        # Always keep the file (empty when drained): consumers may watch its mtime.
        body = "".join(json.dumps(e) + "\n" for e in remaining)
        self.queue_path.write_text(body, encoding="utf-8")

    def _append_processed(self, entry: dict, result: dict) -> None:
        if self.processed_path is None:
            return
        self.processed_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"id": entry.get("id"), "text": entry.get("text", ""), "result": result}
        with open(self.processed_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(record) + "\n")

    def run_until_stopped(self, stop_fn: Callable[[], bool], poll_interval: float = 0.5, *,
                          queue_ready: threading.Event | None = None) -> None:
        while not stop_fn():
            # Clear before reading so an append between the read and wait still wakes us.
            if queue_ready is not None:
                queue_ready.clear()
            entries = self._read_queue()
            if not entries:
                if queue_ready is None:
                    time.sleep(poll_interval)
                else:
                    queue_ready.wait(poll_interval)  # also poll for external meet say writers
                continue
            # One entry per iteration: the queue may grow while we speak.
            head = entries[0]
            text = (head.get("text") or "").strip()
            result = {"ok": True, "bytes_written": 0, "duration_ms": 0.0}
            text_ready, queued_at = head.get("text_ready_at_ns"), head.get("queued_at_ns")
            timings = None
            if isinstance(text_ready, int) and isinstance(queued_at, int):
                timings = {
                    "text_ready_at_ns": text_ready, "queued_at_ns": queued_at,
                    "realtime_sent_at_ns": None, "response_created_at_ns": None,
                    "first_audio_frame_received_at_ns": None, "first_pcm_at_ns": None,
                }
            if text:
                try:
                    result = self.session.speak(text, **({"timings": timings} if timings is not None else {}))
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
            if timings is not None:
                record = {"id": head["id"], "ok": result["ok"], **timings}
                with contextlib.suppress(OSError):
                    print("MEET_SPEECH_TIMING " + json.dumps(record), file=sys.stderr, flush=True)
            self._append_processed(head, result)
            # Re-read (new entries may have arrived), then drop the head by position or id — under the
            # queue lock, so a meet_say append cannot land between the re-read and the rewrite.
            with say_queue_lock(self.queue_path):
                latest = self._read_queue()
                self._rewrite_queue(latest[1:] if latest and latest[0].get("id") == head.get("id")
                                    else [e for e in latest if e.get("id") != head.get("id")])
