"""Trusted Soar Meet routes and profile-scoped browser credentials."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from hermes_constants import get_hermes_home
from utils import atomic_json_write

ORIGIN = "https://meet.soar.inc"
_CONVERSATION = re.compile(r"/chat/[A-Za-z0-9_-]{1,128}/?")


def meeting_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "meet.soar.inc" or not _CONVERSATION.fullmatch(parsed.path):
        raise ValueError("Use an https://meet.soar.inc/chat/<conversation-id> conversation link.")
    # The frontend's join=1 path checks for an active call before joining.
    return ORIGIN + parsed.path.rstrip("/") + "?join=1"


def workspace() -> Path:
    return Path(get_hermes_home()) / "workspace" / "soar-meet"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def request_identity(request) -> dict:
    response = request.get(f"{ORIGIN}/api/auth/session", timeout=10_000, max_redirects=0)
    if not response.ok:
        raise ValueError("Soar session is invalid or expired. Run soar-meet auth again.")
    user = response.json().get("user") or {}
    if not user.get("id") or not user.get("name"):
        raise ValueError("Soar did not return a signed-in participant.")
    return {"id": user["id"], "name": user["name"]}


def identity(context) -> dict:
    return request_identity(context.request)


def save_auth(context) -> dict:
    user = identity(context)
    root = workspace()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_json_write(root / "auth.json", {"user": user, "storage_state": context.storage_state()}, mode=0o600)
    return user
