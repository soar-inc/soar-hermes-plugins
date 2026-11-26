"""Presentation renews the selected user's FlowMind session without changing the URL."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.google_meet import flowmind_session as fm
from plugins.google_meet import presentation

BASE = "https://build.soar.inc"
TICKET = f"{BASE}/ticket-management/tickets/COMS-5?tab=activity#comments"


def response(status=200, email="dev@soar.inc"):
    return SimpleNamespace(status=status, json=lambda: {"email": email}, dispose=Mock())


def browser(*responses):
    return SimpleNamespace(request=SimpleNamespace(get=Mock(side_effect=responses)), new_page=Mock())


@pytest.mark.parametrize("path", ["/dashboard", "/projects/COMS", "/ticket-management/tickets/COMS-5"])
def test_flowmind_session_applies_to_every_flowmind_page(path, tmp_path, monkeypatch):
    monkeypatch.setattr("plugins.google_meet.chrome_profile.chrome_profile_path", lambda: tmp_path)
    assert fm.FlowMindSession.for_url(object(), BASE + path).binding.parent == tmp_path
    assert fm.FlowMindSession.for_url(object(), f"https://build.soar.inc.example.org{path}") is None


def test_restores_the_selected_account_through_the_normal_login_in_a_separate_tab(tmp_path):
    context = browser(response(), response(401), response(email="DEV@soar.inc"))
    fm.bind_flowmind_account(context, tmp_path)
    session = fm.FlowMindSession(context, tmp_path)

    session.ensure()
    login = context.new_page.return_value
    login.goto.assert_called_once_with(f"{BASE}/login?callbackUrl=%2Fdashboard",
                                      wait_until="domcontentloaded", timeout=10_000)
    login.close.assert_called_once()
    assert context.request.get.call_count == 3
    assert session.binding.stat().st_mode & 0o777 == 0o600
    assert "dev@soar.inc" not in session.binding.read_text()


@pytest.mark.parametrize("renewed, error", [
    (response(email="someone-else@soar.inc"), "different account"),
    (response(401), "sign in again"),
])
def test_refuses_switched_accounts_and_interactive_sign_in(tmp_path, renewed, error):
    context = browser(response(), response(401), renewed)
    fm.bind_flowmind_account(context, tmp_path)
    with pytest.raises(fm.FlowMindSessionError, match=error):
        fm.FlowMindSession(context, tmp_path).ensure()
    context.new_page.return_value.close.assert_called_once()


def test_does_not_start_another_login_when_session_check_is_unavailable(tmp_path):
    context = browser(response(), response(503))
    fm.bind_flowmind_account(context, tmp_path)
    with pytest.raises(fm.FlowMindSessionError, match="verify"):
        fm.FlowMindSession(context, tmp_path).ensure()
    context.new_page.assert_not_called()


def test_presentation_opens_the_exact_requested_flowmind_url(monkeypatch):
    from tools.bot_desktop import lease

    tab = Mock(url=TICKET)
    tab.goto.return_value = Mock(ok=True, status=200)
    meet = Mock(context=Mock())
    meet.context.new_page.return_value = tab
    meet.evaluate.side_effect = [None, "armed"]
    present = Mock()
    session = Mock()

    monkeypatch.setattr(lease, "assert_agent_may_act", lambda: None)
    monkeypatch.setattr(presentation, "_control", lambda page, label: present if label == presentation._PRESENT else None)
    monkeypatch.setattr(presentation, "_presentation_url", lambda url: url)
    monkeypatch.setattr(fm.FlowMindSession, "for_url", Mock(return_value=session))
    controller = presentation.Presentation(meet, Mock(), capture_key="test")

    controller.start(TICKET, "COMS-5")

    session.ensure.assert_called_once_with()
    tab.goto.assert_called_once_with(TICKET, wait_until="domcontentloaded", timeout=15_000)
    assert controller.url == TICKET
