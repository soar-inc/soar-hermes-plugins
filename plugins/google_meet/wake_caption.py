"""Answer opt-in wake questions collected from live Google Meet captions."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from queue import Empty, SimpleQueue

_PUNCTUATION = " ,.!?;:—–-"


def _answer(question: str) -> str:
    from agent.auxiliary_client import call_llm
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        model_config = load_config().get("model")
        model = model_config.get("default") if isinstance(model_config, dict) else model_config
        runtime = resolve_runtime_provider(target_model=model)
        response = call_llm(
            task="meet_wake",
            provider=runtime["provider"],
            model=model,
            base_url=runtime.get("base_url"),
            api_key=runtime.get("api_key"),
            api_mode=runtime.get("api_mode"),
            main_runtime=runtime,
            timeout=20,
            messages=[
                {"role": "system", "content": "Answer the meeting attendee in one short spoken sentence. "
                 "Use general knowledge. Do not repeat the wake phrase."},
                {"role": "user", "content": question},
            ],
        )
        return " ".join((response.choices[0].message.content or "").split())[:650]
    finally:
        reset_secret_scope(token)


class CaptionWake:
    def __init__(self, bot_name: str, out_dir: Path, *, wake_session: str | None = None,
                 wake_name: str | None = None, wake_aliases: tuple[str, ...] = ()) -> None:
        word = wake_name
        if word is None:
            from hermes_cli.config import load_config_readonly

            options = load_config_readonly().get("plugins", {}).get("entries", {}).get("google_meet", {})
            word = options.get("wake_word", "Horizon")
            aliases = options.get("wake_aliases", [])
            if isinstance(aliases, list):
                wake_aliases = (*wake_aliases, *aliases)
        if not isinstance(word, str) or not word.isalpha():
            word = "Horizon"
        words = [word]
        words.extend(alias for alias in wake_aliases if isinstance(alias, str) and alias.isalpha())
        alternatives = "|".join(re.escape(word) for word in words)
        self._wake = re.compile(rf"\bhey[\s,.:!?—–-]+(?:{alternatives})\b", re.IGNORECASE)
        self._split_wake = re.compile(rf"(?:{alternatives})\b", re.IGNORECASE)
        self._bot_names = {bot_name.strip().casefold(), "you"} - {""}
        self._queue_path = out_dir / "say_queue.jsonl"
        self._wake_session = wake_session
        self._speaker: str | None = None
        self._parts: dict[int, str] = {}
        self._marker_row = -1
        self._last: tuple[str, int, str, float] | None = None
        self._updated = 0.0
        self._working = False
        self._results: SimpleQueue[tuple[str, str | None]] = SimpleQueue()

    def observe(self, entry: dict, now: float) -> None:
        row = entry.get("row")
        text = " ".join(str(entry.get("text", "")).split())
        speaker = str(entry.get("speaker", "")).strip().casefold()
        if not isinstance(row, int) or not text or speaker in self._bot_names or speaker.endswith(" (you)"):
            return

        matches = list(self._wake.finditer(text))
        previous = " ".join(str(entry.get("previous", "")).split())
        fresh = len(matches) > len(list(self._wake.finditer(previous)))
        after_wake = text[matches[-1].end():].lstrip(_PUNCTUATION) if matches else ""
        # Captions can split the two wake words across adjacent rows.
        if not matches and self._last is not None:
            last_speaker, last_row, last_text, last_at = self._last
            split = self._split_wake.match(text)
            if (split and row > last_row and speaker == last_speaker and now - last_at < 3
                    and re.search(r"\bhey[,.:!?]?$", last_text, re.IGNORECASE)):
                fresh = True
                after_wake = text[split.end():].lstrip(_PUNCTUATION)
        self._last = (speaker, row, text, now)

        if fresh:
            self._speaker, self._marker_row = speaker, row
            self._parts = {row: after_wake}
            self._updated = now
        elif self._speaker == speaker and row >= self._marker_row and now - self._updated < 6:
            if row == self._marker_row:
                if matches:
                    self._parts[row] = after_wake
                elif (split := self._split_wake.match(text)):
                    self._parts[row] = text[split.end():].lstrip(_PUNCTUATION)
                else:
                    return
            elif not matches:
                self._parts[row] = text
            else:
                return
            self._updated = now

    def poll(self, state, now: float) -> None:
        if self._working:
            try:
                answer, error = self._results.get_nowait()
            except Empty:
                return
            self._working = False
            if answer:
                from plugins.google_meet.process_manager import append_say
                try:
                    append_say(self._queue_path, answer)
                except (OSError, TimeoutError):
                    error = "Could not queue the spoken response."
            state.set(wake_error=error)

        if self._speaker is None:
            return
        question = " ".join(part for _, part in sorted(self._parts.items()) if part)[:500]
        if now - self._updated < (2 if question else 6):
            return
        self._speaker, self._parts = None, {}
        if not question:
            return
        state.set(wake_questions=state.wake_questions + 1, wake_error=None)
        self._working = True
        threading.Thread(target=self._answer, args=(question,), name="meet-wake", daemon=True).start()

    def _answer(self, question: str) -> None:
        try:
            if self._wake_session:
                from plugins.google_meet.wake_chat import answer_in_chat

                answer = answer_in_chat(self._wake_session, question)
            else:
                answer = _answer(question)
            self._results.put((answer, None if answer else "The profile model returned an empty answer."))
        except Exception as exc:
            from plugins.google_meet.wake_chat import WakeChatError

            error = str(exc) if isinstance(exc, WakeChatError) else f"Profile model request failed ({type(exc).__name__})."
            self._results.put(("", error))
