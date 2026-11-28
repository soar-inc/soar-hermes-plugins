"""Run the normal Hermes tool loop for a presentation-enabled Meet participant."""

from __future__ import annotations

import json
import sys
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from threading import Lock
from typing import Any

from .model_timing import measure_model_request, measure_preparation
from .voice_tools import PRESENTATION_TOOLSET, voice_tools

_INSTRUCTIONS = (
    "You are Dev, speaking aloud as a participant in the current Google Meet. "
    "Treat the participant's spoken request as the user prompt and respond briefly. "
    "Use the request, conversation context, and available tool descriptions to decide "
    "whether to answer aloud or take an action the participant asked for. "
    "Use URLs supplied by the participant or verified by a lookup. "
    "Ask a brief clarifying question when the intended action or destination is unclear. "
    "Only say an action succeeded after its tool confirms it. Do not read credentials or long URLs aloud."
)


# The first conversation imports these after AIAgent construction on the Meet path.
_FIRST_TURN_IMPORTS = (
    "agent.conversation_loop",
    "agent.subagent_lifecycle",
    "agent.relay_runtime",
    "nemo_relay",
    "hermes_state",
    "hermes_cli.observability.relay_shared_metrics",
    "agent.chat_completion_helpers",
    "agent.side_question_lease",
    "hermes_cli.profiles",
    "tools.bot_desktop.runtime",
    "agent.coding_context",
    "tools.bot_mode_dm",
    "agent.system_prompt",
    "agent.review_idle_queue",
    "gateway.bot_loop_guard",
    "agent.bot_chain",
    "hermes_cli.skin_engine",
)


@contextmanager
def _timed(timings: dict[str, Any], step: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[f"{step}_ms"] = round((time.perf_counter() - started) * 1000, 2)


def _log_setup(phase: str, timings: dict[str, Any], error: str | None) -> None:
    record = {"phase": phase, "ok": error is None, **timings}
    if error is not None:
        record["error_type"] = error
    print("MEET_AGENT_TIMING " + json.dumps(record), file=sys.stderr, flush=True)


def _register_tools(presentation) -> None:
    from tools.registry import registry

    if not presentation.native_picker:
        registry.register(
            name="meet_present_tab", toolset=PRESENTATION_TOOLSET,
            schema={
                "name": "meet_present_tab",
                "description": (
                    "Open and share a requested HTTPS page with everyone in Dev's current Google Meet call. "
                    "Use this when the participant wants others in the call to see the page. "
                    "Use a URL supplied by the participant or returned by a relevant lookup tool. "
                    "Returns once the tab capture is confirmed or reports why it could not start."
                ),
                "parameters": {
                    "type": "object", "required": ["url"],
                    "properties": {
                        "url": {"type": "string", "description": "Absolute public HTTPS URL to present."},
                        "title": {"type": "string", "description": "Short name of the requested page."},
                    },
                    "additionalProperties": False,
                },
            },
            handler=lambda args, **_: presentation.request("present", args),
            execution_timeout=50,
        )
    registry.register(
        name="meet_stop_presenting", toolset=PRESENTATION_TOOLSET,
        schema={
            "name": "meet_stop_presenting",
            "description": "Stop Dev's presentation and keep Dev in the current Google Meet.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        handler=lambda args, **_: presentation.request("stop", args),
        execution_timeout=50,
    )


@dataclass
class _WarmedAgent:
    settings: tuple
    agent: Any
    db: Any
    cleanup: ExitStack


class MeetAgentTurn:
    """The agent runs on the caption worker. Playwright remains on the meeting thread."""

    def __init__(self, presentation, session_id: str | None) -> None:
        self.presentation = presentation
        self.session_id = session_id
        self.history: list[dict] = []
        self._setup_lock = Lock()
        self._warm_lock = Lock()
        self._warmed: _WarmedAgent | None = None
        self._first_turn_started = False
        self._closed = False
        _register_tools(presentation)

    @contextmanager
    def _prepare_runtime(self, timings: dict[str, Any]):
        with ExitStack() as scope:
            with ExitStack() as setup:
                with _timed(timings, "setup_lock_wait"):
                    setup.enter_context(self._setup_lock)
                with _timed(timings, "imports"):
                    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
                    from hermes_cli.config import load_config_readonly
                    from hermes_cli.runtime_provider import resolve_runtime_provider
                    from hermes_constants import get_hermes_home
                    from run_agent import AIAgent
                    from tools.mcp_tool_discovery import discover_mcp_tools

                with _timed(timings, "profile"):
                    home = Path(get_hermes_home())
                    secrets = build_profile_secret_scope(home)
                    token = set_secret_scope(secrets, profile_home=str(home))
                    scope.callback(reset_secret_scope, token)
                    config = load_config_readonly()
                    model_config = config.get("model")
                    model = model_config.get("default") if isinstance(model_config, dict) else model_config
                    meet_config = config.get("plugins", {}).get("entries", {}).get("google_meet", {})
                    meet_model = meet_config.get("model") if isinstance(meet_config, dict) else None
                    if isinstance(meet_model, str) and meet_model.strip():
                        model = meet_model.strip()
                with _timed(timings, "provider"):
                    runtime = resolve_runtime_provider(target_model=model)
                with _timed(timings, "flowmind_discovery"):
                    discover_mcp_tools(["flowmind"])
                yield home, config, secrets, model, runtime, AIAgent

    def _agent_options(self, model, runtime, tools, instructions) -> dict:
        return dict(
            model=model, provider=runtime["provider"], api_mode=runtime.get("api_mode"),
            base_url=runtime.get("base_url"), api_key=runtime.get("api_key"),
            enabled_toolsets=tools, ephemeral_system_prompt=_INSTRUCTIONS + instructions,
            quiet_mode=True, skip_context_files=True, skip_memory=True, skip_background_review=True,
            session_id=self.session_id, platform="local",
            max_iterations=12, run_budget_seconds=90,
        )

    def _build_agent(self, agent_type, options: dict, stack: ExitStack, timings: dict[str, Any]):
        db = None
        if self.session_id:
            from hermes_state import SessionDB
            from plugins.meeting_wake_chat import WakeChatError

            with _timed(timings, "chat_open"):
                db = SessionDB()
                stack.callback(db.close)
                if not db.get_session(self.session_id):
                    raise WakeChatError("The meeting's Hermes chat no longer exists.")
        with _timed(timings, "agent_init"), measure_preparation(timings, "agent_init_preparation"):
            agent = agent_type(**options, session_db=db)
            stack.callback(agent.close)
        return agent, db

    def close(self) -> None:
        """Dispose of a warmed agent if the call ends before its first request."""
        with self._warm_lock:
            self._closed = True
            warmed, self._warmed = self._warmed, None
        if warmed is not None:
            warmed.cleanup.close()

    def warm_up(self) -> None:
        """Prepare one agent for the first request without starting a conversation."""
        from plugins.meeting_wake_chat import validate_wake_chat

        timings: dict[str, Any] = {}
        error = None
        try:
            with (
                _timed(timings, "total"),
                self._prepare_runtime(timings) as (home, config, secrets, model, runtime, agent_type),
                ExitStack() as resources,
            ):
                if self._first_turn_started or self._closed:
                    return
                if self.session_id:
                    validate_wake_chat(self.session_id)
                with ExitStack() as tool_scope:
                    with _timed(timings, "voice_tools"):
                        tools, instructions = tool_scope.enter_context(voice_tools(config, home, self.presentation))
                    options = self._agent_options(model, runtime, tools, instructions)
                    settings = deepcopy((home, config, secrets, options))
                    agent, db = self._build_agent(agent_type, options, resources, timings)
                with _timed(timings, "first_turn_imports"):
                    module_timings = timings["first_turn_imports_by_module_ms"] = {}
                    for module_name in _FIRST_TURN_IMPORTS:
                        started = time.perf_counter()
                        try:
                            import_module(module_name)
                        except Exception as exc:
                            timings.setdefault("first_turn_import_errors", {})[module_name] = type(exc).__name__
                        finally:
                            module_timings[module_name] = round((time.perf_counter() - started) * 1000, 2)
                if runtime.get("api_mode") == "chat_completions":
                    # The SDK loads its chat resources lazily when the first stream opens.
                    with _timed(timings, "openai_chat_sdk_import"):
                        try:
                            import_module("openai.resources.chat")
                        except Exception as exc:
                            timings["openai_chat_sdk_import_error"] = type(exc).__name__
                # Cache the profile language and its English fallback before the first turn.
                with _timed(timings, "language_catalog"):
                    try:
                        from agent.i18n import DEFAULT_LANGUAGE, _load_catalog, get_language

                        for language in dict.fromkeys((get_language(), DEFAULT_LANGUAGE)):
                            _load_catalog(language, str(home))
                    except Exception as exc:
                        timings["language_catalog_error"] = type(exc).__name__
                with self._warm_lock:
                    if not self._closed and self._warmed is None:
                        self._warmed = _WarmedAgent(settings, agent, db, resources.pop_all())
        except Exception as exc:
            error = type(exc).__name__
        finally:
            _log_setup("warm_up", timings, error)

    def __call__(self, question: str) -> str:
        from plugins.meeting_wake_chat import WakeChatError, validate_wake_chat

        if question.lstrip().startswith("/"):
            raise WakeChatError("A spoken question cannot run a Hermes slash command.")
        timings: dict[str, Any] = {}
        error = None
        try:
            with (
                _timed(timings, "total"),
                self._prepare_runtime(timings) as (home, config, secrets, model, runtime, agent_type),
                ExitStack() as stack,
            ):
                self._first_turn_started = True
                with self._warm_lock:
                    if self._closed:
                        raise WakeChatError("The Google Meet call has ended.")
                    warmed, self._warmed = self._warmed, None
                if warmed is not None:
                    stack.callback(warmed.cleanup.close)
                if self.session_id:
                    validate_wake_chat(self.session_id)
                with _timed(timings, "voice_tools"):
                    tools, instructions = stack.enter_context(voice_tools(config, home, self.presentation))
                options = self._agent_options(model, runtime, tools, instructions)
                # Recheck profile settings, credentials and the guarded toolset on the worker.
                reuse = warmed is not None and warmed.settings == (home, config, secrets, options)
                timings["agent_reused"] = reuse
                if reuse:
                    agent, db = warmed.agent, warmed.db
                else:
                    if warmed is not None:
                        warmed.cleanup.close()
                    agent, db = self._build_agent(agent_type, options, stack, timings)
                with _timed(timings, "chat_history"):
                    history = self.history
                    if db is not None:
                        if not db.get_session(self.session_id):
                            raise WakeChatError("The meeting's Hermes chat no longer exists.")
                        history = db.get_messages_as_conversation(self.session_id, include_ancestors=True)
                with measure_model_request(agent, timings) as final_text_ready:
                    with _timed(timings, "model_turn"):
                        result = agent.run_conversation(question, conversation_history=history)
                    if not self.session_id and isinstance(result.get("messages"), list):
                        self.history = [item for item in result["messages"] if item.get("role") != "system"]
                    reply = " ".join(str(result.get("final_response") or "").split())[:650]
                    if reply:
                        final_text_ready()
                    return reply
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            _log_setup("turn", timings, error)
