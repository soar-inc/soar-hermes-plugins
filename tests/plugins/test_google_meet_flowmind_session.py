"""The presentation login uses a fresh service token in the same browser context."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.google_meet import flowmind_session as fm

DASHBOARD = "https://build.soar.inc/ticket-management/initiatives/dashboard"


def test_only_the_approved_dashboard_opts_in(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    secret = tmp_path / "workspace" / "meetings" / "flowmind-client-secret"
    assert fm.FlowMindSession.for_url(object(), DASHBOARD) is None
    secret.parent.mkdir(parents=True)
    secret.touch(mode=0o600)
    assert fm.FlowMindSession.for_url(object(), DASHBOARD).secret_path == secret
    assert fm.FlowMindSession.for_url(object(), "https://build.soar.inc/dashboard") is None
    assert fm.FlowMindSession.for_url(object(), DASHBOARD.replace("build.soar.inc", "other.soar.inc")) is None


def test_secret_is_private_and_cannot_be_a_symlink(tmp_path):
    secret = tmp_path / "client-secret"
    secret.write_text("test-secret\n")
    secret.chmod(0o600)
    assert fm._read_secret(secret) == "test-secret"
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        fm._read_secret(secret)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        fm._read_secret(link)


def test_renews_before_expiry_and_stops_if_the_grant_stays_unavailable(tmp_path, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(fm.time, "time", lambda: clock[0])
    mint = Mock(side_effect=["first-token", "second-token", ValueError(), ValueError()])
    monkeypatch.setattr(fm, "_mint_token", mint)
    post = Mock(side_effect=[
        SimpleNamespace(status=200, json=lambda: {"expires_at": 1300}, dispose=Mock()),
        SimpleNamespace(status=200, json=lambda: {"expires_at": 1540}, dispose=Mock()),
    ])
    session = fm.FlowMindSession(SimpleNamespace(request=SimpleNamespace(post=post)), tmp_path / "secret")

    session.ensure()
    clock[0] = 1239
    session.maintain()
    assert post.call_count == 1
    clock[0] = 1240
    session.maintain()
    assert [call.kwargs["headers"]["Authorization"] for call in post.call_args_list] == [
        "Bearer first-token", "Bearer second-token",
    ]
    assert all(call.kwargs["max_redirects"] == 0 for call in post.call_args_list)

    clock[0] = 1480
    session.maintain()
    clock[0] = 1540
    with pytest.raises(fm.FlowMindSessionError, match="read-only FlowMind session"):
        session.maintain()
