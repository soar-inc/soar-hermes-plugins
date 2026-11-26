"""Renew Dev's read-only FlowMind presentation session in the Meet browser."""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_ORIGIN = "https://build.soar.inc"
_SESSION_URL = f"{_ORIGIN}/api/auth/presentation-session"
_VIEW_URL = f"{_ORIGIN}/api/auth/presentation-view"
_TOKEN_URL = "https://soar-id-277347409468.me-central2.run.app/token"
_CLIENT_ID = "flowmind-dev-presenter"


class FlowMindSessionError(RuntimeError):
    """Safe to report in the meeting without including any credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_secret(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as secret_file:
        info = os.fstat(secret_file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("credential permissions")
        secret = secret_file.read(4097).decode("ascii").strip()
    if not secret or len(secret) > 4096 or any(not 33 <= ord(char) <= 126 for char in secret):
        raise ValueError("credential format")
    return secret


def _mint_token(path: Path) -> str:
    credentials = base64.b64encode(f"{_CLIENT_ID}:{_read_secret(path)}".encode("ascii")).decode("ascii")
    request = Request(
        _TOKEN_URL,
        data=urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with build_opener(_NoRedirect()).open(request, timeout=5) as response:
        payload = json.loads(response.read(16_385))
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.isascii() or not 1 <= len(token) <= 3500:
        raise ValueError("token format")
    return token


class FlowMindSession:
    def __init__(self, context, secret_path: Path):
        self.context = context
        self.secret_path = secret_path
        self.expires_at = 0
        self.renew_after = 0

    @classmethod
    def for_url(cls, context, url: str) -> FlowMindSession | None:
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc, parsed.path.rstrip("/")) != (
            "https", "build.soar.inc", "/ticket-management/initiatives/dashboard"
        ):
            return None

        from hermes_constants import get_hermes_home

        secret_path = Path(get_hermes_home()) / "workspace" / "meetings" / "flowmind-client-secret"
        return cls(context, secret_path) if os.path.lexists(secret_path) else None

    @property
    def view_url(self) -> str:
        return _VIEW_URL

    def ensure(self, *, force: bool = False) -> None:
        if not force and time.time() < self.renew_after:
            return
        try:
            token = _mint_token(self.secret_path)
            response = self.context.request.post(
                _SESSION_URL, headers={"Authorization": f"Bearer {token}"},
                max_redirects=0, timeout=5_000,
            )
            try:
                if response.status != 200:
                    raise ValueError("session refused")
                expires_at = response.json()["expires_at"]
                now = time.time()
                if type(expires_at) is not int or not now + 5 < expires_at <= now + 300:
                    raise ValueError("invalid expiry")
            finally:
                response.dispose()
        except Exception:
            raise FlowMindSessionError("Dev couldn't renew its read-only FlowMind session.") from None
        self.expires_at = expires_at
        self.renew_after = expires_at - min(60, (expires_at - now) / 2)

    def maintain(self) -> None:
        try:
            self.ensure()
        except FlowMindSessionError:
            now = time.time()
            if now >= self.expires_at:
                raise
            self.renew_after = min(self.expires_at, now + 10)
