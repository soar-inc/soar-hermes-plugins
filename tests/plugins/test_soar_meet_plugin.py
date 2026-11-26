"""Soar browser seam and profile-scoped state."""

import argparse
import json
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.google_meet import wake_caption
from plugins.soar_meet import bot, cli, session
from tests.waiting import wait_until


def test_url_uses_native_active_call_join_and_discards_other_parameters():
    assert session.meeting_url("https://meet.soar.inc/chat/test-conversation?seq=12") == (
        "https://meet.soar.inc/chat/test-conversation?join=1"
    )
    for bad in (
        "https://meet.soar.inc/",
        "https://meet.soar.inc.evil.example/chat/123",
        "https://meet.soar.inc@evil.example/chat/123",
        "https://meet.soar.inc/chat/a%2Fb",
        "http://meet.soar.inc/chat/123",
    ):
        with pytest.raises(ValueError):
            session.meeting_url(bad)


def test_auth_only_saves_a_verified_soar_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "workspace", lambda: tmp_path)
    context = Mock()
    context.request.get.return_value.ok = False
    with pytest.raises(ValueError, match="invalid or expired"):
        session.save_auth(context)
    auth = tmp_path / "auth.json"
    assert not auth.exists()
    context.request.get.return_value.ok = True
    context.request.get.return_value.json.return_value = {"user": {"id": "bot-1", "name": "Long Horizon"}}
    context.storage_state.return_value = {"cookies": []}
    assert session.save_auth(context)["id"] == "bot-1"
    assert json.loads(auth.read_text())["storage_state"] == {"cookies": []}
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600


def test_wake_requires_realtime_before_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "workspace", lambda: tmp_path)
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    cmd = parser.parse_args(["join", "https://meet.soar.inc/chat/room1", "--wake"])
    with pytest.raises(ValueError, match="requires --mode realtime"):
        cli._join(cmd)


def test_state_shows_soar_identity_and_keeps_transcript_in_its_run(tmp_path):
    state = bot._SoarState(tmp_path, "room1", "https://meet.soar.inc/chat/room1?join=1")
    state.set(signed_in_as="Long Horizon", in_call=True)
    state.record_caption("Caller", "Hey Dev, what is the capital of India?")
    data = json.loads((tmp_path / "status.json").read_text())
    assert data["signedInAs"] == "Long Horizon"
    assert data["inCall"] is True
    assert data["transcriptLines"] == 1
    assert "Hey Dev" in (tmp_path / "transcript.txt").read_text()


@pytest.mark.parametrize(("wake_name", "voice_leave"), [("Dev", False), ("Horizon", False), ("Dev", True)])
def test_soar_drains_split_wake_phrase(tmp_path, monkeypatch, wake_name, voice_leave):
    state = bot._SoarState(tmp_path, "room1", "https://meet.soar.inc/chat/room1?join=1")
    answer = Mock(return_value="Four.")
    monkeypatch.setattr(wake_caption, "_answer", answer)
    occupancy = Mock(return_value=("call-1", 2))
    monkeypatch.setattr(bot, "_call_occupancy", occupancy)
    ticks = iter((0, 0, 0, 1, 2, 4))
    monkeypatch.setattr(bot, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    page = Mock()
    page.get_by_role.return_value.first.is_visible.return_value = True
    page.is_closed.return_value = False
    stop = {"stop": False}

    def caption(row, text):
        return {"row": row, "speaker": "Caller", "text": text, "previous": ""}

    request = "leave the call" if voice_leave else "what is two plus two?"
    page.evaluate.side_effect = [
        {"open": True, "entries": []},
        {"open": True, "entries": [caption(1, "Hey")]},
        {"open": True, "entries": [caption(2, f"{wake_name}, {request}")]},
        {"open": True, "entries": []},
    ]

    def finish_after_trigger(_ms):
        if state.wake_questions:
            stop["stop"] = True

    page.wait_for_timeout.side_effect = finish_after_trigger
    bot._drain(page, state, {}, "Long Horizon", stop, conversation_id="room1")
    occupancy.assert_called_once_with(page, "room1")
    if voice_leave:
        assert stop["stop"]
        assert state.leave_reason == "voice_requested"
        answer.assert_not_called()
    else:
        assert wait_until(lambda: answer.call_count == 1, timeout=3)
        answer.assert_called_once_with("what is two plus two?")
