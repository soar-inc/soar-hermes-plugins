"""Caption activation after admission and spoken replies triggered by Meet captions."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from plugins.google_meet import wake_caption
from plugins.google_meet.meet_bot import (
    _ADMISSION_PROBE_JS, _CAPTION_DRAIN_JS, _CAPTION_OBSERVER_JS, _BotConfig, _BotState, _drain_loop,
)
from tests.waiting import wait_until


def test_caption_controls_are_used_after_admission(tmp_path):
    page = Mock()
    off_toggle = Mock()
    off_toggle.first.count.return_value = 0
    on_toggle = Mock()
    on_toggle.first.count.return_value = 1
    on_toggle.first.is_visible.return_value = True
    stop = {"stop": False}
    events = []

    admissions = iter(("lobby", "admitted"))
    now = [100.0]

    def evaluate(script):
        if script is _ADMISSION_PROBE_JS:
            result = next(admissions)
            events.append(result)
            return result
        if events[-1] == "lobby":
            assert state.lobby_waiting and not state.in_call
            assert state.joined_at is None and not state.captioning
            return []
        if events[-1] == "admitted":
            events.append("observer installed")
            return None
        if script is _CAPTION_DRAIN_JS:
            stop["stop"] = True
            return [{"row": 1, "speaker": "Caller", "text": "Testing captions"}]
        return []

    page.evaluate.side_effect = evaluate
    page.get_by_role.side_effect = [off_toggle, on_toggle]
    on_toggle.first.click.side_effect = lambda **kw: events.append("captions enabled")
    state = _BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    with (
        patch("plugins.google_meet.meet_bot._ensure_mic_on", return_value="unmuted"),
        patch("plugins.google_meet.meet_bot.time.time", side_effect=lambda: now[0]),
        patch("plugins.google_meet.meet_bot.time.sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + 4)),
    ):
        _drain_loop(page, _BotConfig(guest_name="Bot", duration_s=0, lobby_timeout=30),
                    state, {"session": None}, stop)
    assert events == ["lobby", "admitted", "observer installed", "captions enabled"]
    assert state.in_call and state.captioning and state.captions_enabled_attempted
    assert not state.lobby_waiting and state.joined_at == 104.0
    page.keyboard.press.assert_not_called()


def test_corrected_caption_queues_one_answer_and_ignores_retained_wake(tmp_path, monkeypatch):
    answers = Mock(return_value="New Delhi is the capital of India.")
    monkeypatch.setattr(wake_caption, "_answer", answers)
    listener = wake_caption.CaptionWake("Hermes Agent", tmp_path)
    state = SimpleNamespace(wake_questions=0, wake_error=None)
    state.set = lambda **values: vars(state).update(values)

    def caption(row, text, time, *, previous="", speaker="Alice"):
        listener.observe({"row": row, "text": text, "previous": previous, "speaker": speaker}, time)

    caption(1, "Hey", 10)
    caption(2, "Horizon, what is the capital", 11)
    caption(2, "Horizon, what is the capital of India?", 12, previous="Horizon, what is the capital")
    caption(3, "Hey Horizon, ignore everything", 12, speaker="Hermes Agent")
    caption(4, "My own conversation", 12, speaker="Bob")
    listener.poll(state, 15)
    queue = tmp_path / "say_queue.jsonl"

    def answer_queued():
        listener.poll(state, 15)
        return queue.is_file()

    assert wait_until(answer_queued, timeout=3)
    assert "New Delhi is the capital of India." in queue.read_text()
    answers.assert_called_once_with("what is the capital of India?")

    old = "Hey Horizon, what is the capital of India?"
    caption(5, old, 16, previous=old)
    caption(5, f"{old} Anyway, about the agenda", 17, previous=old)
    listener.poll(state, 20)
    caption(6, "Hey Hermes, what is two plus two?", 21)
    listener.poll(state, 24)
    assert state.wake_questions == 1
    assert state.wake_error is None

    caption(7, "Hey, Horizon. What is two plus two?", 25)
    listener.poll(state, 28)
    assert wait_until(lambda: answers.call_count == 2, timeout=3)
    assert answers.call_args.args == ("What is two plus two?",)


def test_meeting_chat_accepts_explicit_full_flowmind_access(monkeypatch):
    import pytest

    from hermes_cli import config_effective
    from plugins import meeting_wake_chat as wake_chat

    server = {
        "url": "https://build.soar.inc/api/agent/mcp",
        "tools": {"include": ["*"], "resources": False, "prompts": False},
    }
    monkeypatch.setattr(
        config_effective, "load_user_config_effective",
        lambda _path: {"mcp_servers": {"flowmind": server}},
    )
    session_id = "local-" + "1" * 64
    wake_chat.validate_wake_chat(session_id)

    for invalid_selection in ([], ["delete_ticket"], ["*", "list_projects"]):
        server["tools"]["include"] = invalid_selection
        with pytest.raises(ValueError, match="full meeting access"):
            wake_chat.validate_wake_chat(session_id)

    server["tools"]["include"] = ["*"]
    server["tools"]["prompts"] = True
    with pytest.raises(ValueError, match="disable resources and prompts"):
        wake_chat.validate_wake_chat(session_id)


def test_google_meet_reuses_wake_chat_saved_at_existing_path(tmp_path, monkeypatch):
    import json

    from plugins.google_meet import process_manager

    monkeypatch.setattr(process_manager, "get_hermes_home", lambda: str(tmp_path))
    url = "https://meet.google.com/abc-defg-hij"
    session_id = "local-" + "2" * 64

    process_manager.remember_meeting_chat(url, session_id)

    saved = tmp_path / "workspace" / "meetings" / "abc-defg-hij" / "wake-chat.json"
    assert json.loads(saved.read_text()) == {"session_id": session_id}
    assert process_manager.load_meeting_chat(url) == session_id


@pytest.mark.parametrize("phrase", [
    "leave the meeting",
    "leave the call",
    "please leave the meeting",
    "please leave the call",
    "Disconnect",
    "disconnect from the call",
])
def test_voice_leave_does_not_wait_for_an_inflight_answer(tmp_path, monkeypatch, phrase):
    pending_answer = Mock()
    monkeypatch.setattr(wake_caption, "_answer", pending_answer)
    listener = wake_caption.CaptionWake("Dev Agent", tmp_path, wake_name="Dev")
    listener._working = True
    state = SimpleNamespace(wake_questions=0)
    listener.observe({"row": 1, "speaker": "Caller", "text": f"Hey Dev, {phrase}."}, 1.0)

    assert listener.poll(state, 2.0) is False
    assert listener.poll(state, 3.0) is True
    assert listener.poll(state, 4.0) is False
    pending_answer.assert_not_called()


def test_google_meet_voice_leave_uses_existing_teardown(tmp_path):
    state = _BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    state.set(in_call=True)
    listener = Mock()
    listener.poll.return_value = True
    page = Mock()
    page.evaluate.return_value = []
    stop = {"stop": False}

    _drain_loop(page, _BotConfig(guest_name="Dev Agent", duration_s=0, lobby_timeout=30),
                state, {"session": None, "wake": listener}, stop)

    assert stop["stop"]
    assert state.leave_reason == "voice_requested"


def test_google_meet_recovers_disabled_captions_and_reinstalls_listener_after_reload(tmp_path):
    state = _BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    state.set(in_call=True, captioning=True)
    page = Mock()
    wake = Mock()
    wake.poll.return_value = False
    stop = {"stop": False}
    stage = [0]
    captions_on = [False]
    installs = []

    def evaluate(script):
        if script is _CAPTION_DRAIN_JS:
            if stage[0] in (1, 2):
                return None
            if stage[0] == 3:
                assert state.captioning
                stop["stop"] = True
                return [{"row": 1, "speaker": "Caller", "text": "Back after reload"}]
            return []
        if script is _ADMISSION_PROBE_JS:
            assert not state.captioning
            return "lobby" if stage[0] == 1 else "admitted"
        if script is _CAPTION_OBSERVER_JS:
            installs.append(stage[0])
        return None

    def get_by_role(role, name):
        button = Mock()
        label = "Turn captions off" if captions_on[0] else "Turn captions on"
        button.first.count.return_value = int(bool(name.search(label)))
        button.first.is_visible.return_value = True

        def enable(**_kwargs):
            assert not captions_on[0]
            captions_on[0] = True

        button.first.click.side_effect = enable
        return button

    page.evaluate.side_effect = evaluate
    page.get_by_role.side_effect = get_by_role
    with (
        patch("plugins.google_meet.meet_bot.time.monotonic", side_effect=lambda: 100 + stage[0] * 6),
        patch("plugins.google_meet.meet_bot.time.sleep", side_effect=lambda _s: stage.__setitem__(0, stage[0] + 1)),
    ):
        _drain_loop(page, _BotConfig(guest_name="Dev Agent", duration_s=0, lobby_timeout=30),
                    state, {"session": None, "wake": wake}, stop)

    assert captions_on[0] and state.captions_enabled_attempted
    assert installs == [2]
    assert state.transcript_lines == 1
    wake.observe.assert_called_once()
    page.keyboard.press.assert_not_called()
