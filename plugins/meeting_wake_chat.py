"""Route an explicitly selected meeting to a dedicated FlowMind chat session."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

_SESSION_ID = re.compile(r"(?:local-[a-f0-9]{64}|\d{8}_\d{6}_[a-f0-9]{6,12})")
_FLOWMIND_TOOLS = {"list_projects", "list_tickets", "count_tickets", "get_ticket"}


class WakeChatError(RuntimeError):
    """A wake-chat error safe to expose in Meet status."""


def is_wake_chat_session_id(value: object) -> bool:
    return isinstance(value, str) and _SESSION_ID.fullmatch(value) is not None


def validate_wake_chat(session_id: str) -> None:
    if not is_wake_chat_session_id(session_id):
        raise ValueError("--wake-session requires the ID of a dedicated Hermes chat")
    _validate_flowmind()


def _validate_flowmind() -> None:
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import get_hermes_home

    config = load_user_config_effective(Path(get_hermes_home()) / "config.yaml")
    servers = config.get("mcp_servers")
    server = servers.get("flowmind") if isinstance(servers, dict) else None
    if not isinstance(server, dict) or "mcp-flowmind" in servers:
        raise ValueError("Enable the existing FlowMind MCP server named flowmind in this profile.")
    context = config.get("context") or {}
    if str(context.get("engine") or "compressor").strip().lower() != "compressor":
        raise ValueError("Wake chat requires the default context engine to keep tool access restricted.")
    tools = server.get("tools")
    include = tools.get("include") if isinstance(tools, dict) else None
    # A single wildcard explicitly opts the profile into all native FlowMind tools.
    valid_selection = (
        include == ["*"]
        or (
            isinstance(include, list)
            and bool(include)
            and all(isinstance(tool, str) and tool in _FLOWMIND_TOOLS for tool in include)
        )
    )
    url = server.get("url")
    if (not isinstance(url, str) or url.rstrip("/") != "https://build.soar.inc/api/agent/mcp"
            or server.get("enabled") is False or server.get("disabled") is True
            or not valid_selection
            or tools.get("resources") is not False or tools.get("prompts") is not False):
        raise ValueError(
            'Configure enabled flowmind with an explicit read-only tool list or include: ["*"] '
            "for full meeting access, and disable resources and prompts."
        )


def answer_in_chat(session_id: str, question: str) -> str:
    from hermes_constants import get_hermes_home

    try:
        validate_wake_chat(session_id)
    except ValueError as exc:
        raise WakeChatError(str(exc)) from exc
    if question.lstrip().startswith("/"):
        raise WakeChatError("A spoken question cannot run a Hermes slash command.")

    launcher = Path.home() / ".local" / "bin" / "hermes"
    env = {**os.environ, "HERMES_HOME": str(get_hermes_home())}
    env.pop("HERMES_MEET_REALTIME_KEY", None)
    try:
        # The gateway validates the repeated flags against the session's original launch
        # policy. A normal chat with a broader toolset therefore cannot be used here.
        result = subprocess.run(
            [str(launcher), "chat", "--resume", session_id, "--ignore-rules",
             "--toolsets", "mcp-flowmind", "--format", "stream-json", "--query-file", "-"],
            input=question, capture_output=True, text=True, timeout=75, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise WakeChatError("The Hermes chat took too long. Check its history before repeating the question.") from exc
    except OSError as exc:
        raise WakeChatError("Could not start the Hermes chat client.") from exc

    if result.returncode:
        raise WakeChatError("Hermes chat failed. Check the dedicated session ID, launch flags, and FlowMind connection.")
    for line in reversed(result.stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            reply = event.get("text")
            if not event.get("exit_code") and isinstance(reply, str) and reply.strip():
                return " ".join(reply.split())[:650]
            break
    raise WakeChatError("Hermes chat returned no reply.")


def _calendar_title(raw_title, suffix=""):
    from hermes_state import SessionDB

    name, separator, date = raw_title.rpartition(" · ")
    tail = f"{separator}{date}{suffix}" if separator else suffix
    budget = SessionDB.MAX_TITLE_LENGTH - len(tail)
    name = name if separator else raw_title
    if len(name) > budget:
        name = name[:budget - 1].rstrip() + "…"
    return SessionDB.sanitize_title(name + tail)


async def _name_calendar_chat(client, snapshot, raw_title, event_key):
    session_id = snapshot["stored_session_id"]
    for suffix in ("", f" · {event_key[:8]}"):
        title = _calendar_title(raw_title, suffix)
        existing = await client.rpc("session.list", title=title, limit=1)
        sessions = existing.get("sessions", [])
        if sessions:
            if sessions[0].get("session_id") == session_id:
                return
            continue
        await client.rpc(
            "session.mutate", session_id=session_id, request_id=uuid.uuid4().hex,
            expected_revision=snapshot["revision"], operation="rename", payload={"title": title},
        )
        return
    raise WakeChatError("Calendar chat title is already in use.")


async def calendar_chat_session(event_key: str, raw_title: str | None = None, generation: int = 0
                                ) -> tuple[str, str | None, int]:
    from argparse import Namespace
    from hermes_cli.gateway_chat import check_resume_policy
    from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, connect_gateway

    if not re.fullmatch(r"[a-f0-9]{64}", event_key):
        raise ValueError("Invalid Calendar event key.")
    if generation < 0:
        raise ValueError("Invalid Calendar chat generation.")
    _validate_flowmind()
    async with connect_gateway() as client:
        for candidate in range(generation, generation + 8):
            request_id = event_key
            if candidate:
                request_id = hashlib.sha256(f"{event_key}:replacement:{candidate}".encode()).hexdigest()
            try:
                snapshot = await client.rpc(
                    "session.create",
                    request_id=request_id[:32],
                    source="cli",
                    ignore_rules=True,
                    toolsets=["mcp-flowmind"],
                )
                break
            except GatewayRPCError as exc:
                # A deleted session keeps its original request ID retired.
                if str(exc) != "not_found":
                    raise
        else:
            raise WakeChatError("Could not find a reusable Calendar chat after deleted sessions.")
        # The stable request resolves a replacement even if the watcher stopped before saving it.
        check_resume_policy(Namespace(ignore_rules=True, toolsets="mcp-flowmind"), snapshot)
        session_id = snapshot.get("stored_session_id")
        if not is_wake_chat_session_id(session_id):
            raise WakeChatError("Hermes did not return a usable Calendar chat session ID.")
        try:
            await _name_calendar_chat(
                client, snapshot, raw_title or f"Hey Horizon Calendar {event_key[:24]}", event_key,
            )
        except (GatewayClientError, WakeChatError, ValueError):
            return session_id, "Could not update the Calendar chat title. The existing chat will still be used.", candidate
    return session_id, None, candidate


def main() -> int:
    import argparse
    import asyncio
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar-event-key", required=True)
    parser.add_argument("--title-from-stdin", action="store_true")
    parser.add_argument("--calendar-chat-generation", type=int, default=0)
    args = parser.parse_args()
    raw_title = sys.stdin.read(1024) if args.title_from_stdin else None
    try:
        session_id, title_warning, generation = asyncio.run(
            calendar_chat_session(args.calendar_event_key, raw_title, args.calendar_chat_generation)
        )
    except (ValueError, WakeChatError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": "Could not create or resume the Bot's Calendar chat."}))
        return 1
    print(json.dumps({"ok": True, "session_id": session_id, "title_warning": title_warning, "generation": generation}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
