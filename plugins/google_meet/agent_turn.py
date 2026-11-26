"""Run the normal Hermes tool loop for a presentation-enabled Meet participant."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

_TOOLSET = "meet-presentation"
_INSTRUCTIONS = (
    "You are Dev, speaking aloud as a participant in the current Google Meet. "
    "Respond briefly. Treat the spoken request as the user's prompt. "
    "Use meet_present_tab only when the participant asks to show, open for the group, or present a page. "
    "When they provide a URL, use that URL. For a named FlowMind page, use FlowMind's get_dashboard_url "
    "and pass the URL returned by the tool. Ask which page they mean when the destination is unclear. "
    "For a request for information, use relevant tools and answer aloud. "
    "Call meet_stop_presenting when asked to end the presentation. "
    "Say sharing started or stopped only after the tool confirms it. Do not read credentials or long URLs aloud."
)


def _register_tools(presentation) -> None:
    from tools.registry import registry

    registry.register(
        name="meet_present_tab", toolset=_TOOLSET,
        schema={
            "name": "meet_present_tab",
            "description": (
                "Present the requested HTTPS page from Dev's existing Google Meet browser. "
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
        name="meet_stop_presenting", toolset=_TOOLSET,
        schema={
            "name": "meet_stop_presenting",
            "description": "Stop Dev's presentation and keep Dev in the current Google Meet.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        handler=lambda args, **_: presentation.request("stop", args),
        execution_timeout=50,
    )


class MeetAgentTurn:
    """The agent runs on the caption worker. Playwright remains on the meeting thread."""

    def __init__(self, presentation, session_id: str | None) -> None:
        self.session_id = session_id
        self.history: list[dict] = []
        _register_tools(presentation)

    def __call__(self, question: str) -> str:
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
        from hermes_cli.config import load_config_readonly
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_constants import get_hermes_home
        from plugins.meeting_wake_chat import WakeChatError, validate_wake_chat
        from run_agent import AIAgent
        from tools.mcp_tool_discovery import discover_mcp_tools

        if question.lstrip().startswith("/"):
            raise WakeChatError("A spoken question cannot run a Hermes slash command.")
        home = Path(get_hermes_home())
        token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        try:
            config = load_config_readonly()
            model_config = config.get("model")
            model = model_config.get("default") if isinstance(model_config, dict) else model_config
            runtime = resolve_runtime_provider(target_model=model)
            discover_mcp_tools(["flowmind"])
            with ExitStack() as stack:
                db = None
                history = self.history
                if self.session_id:
                    from hermes_state import SessionDB

                    validate_wake_chat(self.session_id)
                    db = SessionDB()
                    stack.callback(db.close)
                    if not db.get_session(self.session_id):
                        raise WakeChatError("The meeting's Hermes chat no longer exists.")
                    history = db.get_messages_as_conversation(self.session_id, include_ancestors=True)
                agent = AIAgent(
                    model=model, provider=runtime["provider"], api_mode=runtime.get("api_mode"),
                    base_url=runtime.get("base_url"), api_key=runtime.get("api_key"),
                    enabled_toolsets=["mcp-flowmind", _TOOLSET], ephemeral_system_prompt=_INSTRUCTIONS,
                    quiet_mode=True, skip_context_files=True, skip_memory=True, skip_background_review=True,
                    session_id=self.session_id, session_db=db, platform="local",
                    max_iterations=12, run_budget_seconds=90,
                )
                stack.callback(agent.close)
                result = agent.run_conversation(question, conversation_history=history)
                if not self.session_id and isinstance(result.get("messages"), list):
                    self.history = [item for item in result["messages"] if item.get("role") != "system"]
                return " ".join(str(result.get("final_response") or "").split())[:650]
        finally:
            reset_secret_scope(token)
