"""Use the first account signed into FlowMind in the Meet Chrome profile."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

FLOWMIND_ORIGIN = "https://build.soar.inc"
_SESSION_URL = f"{FLOWMIND_ORIGIN}/api/auth/presentation-session"
_LOGIN_URL = f"{FLOWMIND_ORIGIN}/login?callbackUrl=%2Fdashboard"
_BINDING = ".hermes-flowmind-account"
_CHECK_INTERVAL = 60


class FlowMindSessionError(RuntimeError):
    """An actionable sign-in error that is safe to read aloud."""


def is_flowmind_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.netloc) == ("https", "build.soar.inc")
    except (TypeError, ValueError):
        return False


def _account(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _signed_in_account(context) -> str | None:
    try:
        response = context.request.get(_SESSION_URL, max_redirects=0, timeout=6_000)
        try:
            if response.status == 401:
                return None
            if response.status != 200:
                raise ValueError("sign-in check failed")
            email = response.json()["email"]
            if not isinstance(email, str) or not email.strip():
                raise ValueError("account missing")
            return _account(email)
        finally:
            response.dispose()
    except Exception:
        raise FlowMindSessionError("Dev couldn't verify its FlowMind sign-in.") from None


def bind_flowmind_account(context, profile: Path) -> None:
    account = _signed_in_account(context)
    if account is None:
        raise FlowMindSessionError("Finish signing in to FlowMind before saving this account.")
    path = None
    try:
        with NamedTemporaryFile(dir=profile, mode="w", encoding="ascii", delete=False) as file:
            path = Path(file.name)
            file.write(f"{account}\n")
        path.replace(profile / _BINDING)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


class FlowMindSession:
    def __init__(self, context, profile: Path):
        self.context = context
        self.binding = profile / _BINDING
        self.next_check = 0.0

    @classmethod
    def for_url(cls, context, url: str) -> FlowMindSession | None:
        if not is_flowmind_url(url):
            return None

        from plugins.google_meet.chrome_profile import chrome_profile_path

        return cls(context, chrome_profile_path())

    def _continue_sign_in(self) -> None:
        # Let FlowMind's existing login page continue via SOAR if this Chrome
        # profile still has SSO. Keep the login tab separate from the share.
        page = self.context.new_page()
        try:
            page.on("popup", lambda popup: popup.close())
            page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=10_000)
        except Exception:
            # A navigation can time out after the new cookies were already set.
            # The caller checks the session itself before sharing anything.
            pass
        finally:
            with suppress(Exception):
                page.close()

    def ensure(self, *, force: bool = False) -> None:
        if not force and time.monotonic() < self.next_check:
            return
        try:
            expected = self.binding.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            expected = ""
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise FlowMindSessionError("Set up Dev's FlowMind account on Bot Screen with meet auth --flowmind.")
        account = _signed_in_account(self.context)
        if account is None:
            self._continue_sign_in()
            account = _signed_in_account(self.context)
        if account is None:
            raise FlowMindSessionError("FlowMind needs the selected account to sign in again on Bot Screen.")
        if not hmac.compare_digest(account, expected):
            raise FlowMindSessionError("FlowMind is signed in as a different account. Check Dev's login on Bot Screen.")
        self.next_check = time.monotonic() + _CHECK_INTERVAL
