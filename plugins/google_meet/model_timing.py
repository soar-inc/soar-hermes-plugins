"""Timing for the model request made by a Google Meet voice turn."""

import sys
from contextlib import contextmanager, suppress
from threading import Lock
from time import perf_counter


# Count only these preparation functions, assigning nested work to its own phase.
_PREPARATION_PHASES = {
    ("importlib._bootstrap", "_find_and_load"): "lazy_imports",
    ("agent.conversation_loop", "_restore_or_build_system_prompt"): "prompt_and_history",
    ("agent.turn_context", "_stage_turn_user_message"): "prompt_and_history",
    ("agent.turn_context", "_hydrate_from_history"): "prompt_and_history",
    ("agent.turn_request_assembly", "assemble_api_request"): "prompt_and_history",
    ("model_tools", "get_tool_definitions"): "tool_schemas",
    ("tools.tool_search", "assemble_tool_defs"): "tool_schemas",
    ("agent.transports.chat_completions", "convert_tools"): "tool_schemas",
    ("agent.transports.anthropic", "convert_tools"): "tool_schemas",
    ("agent.turn_context", "_refresh_mcp_tools_between_turns"): "mcp_tool_refresh",
    ("agent.turn_api_request", "build_request_payload"): "provider_payload",
    ("agent.turn_facade", "run_conversation"): "turn_facade",
    ("agent.turn_facade_lease", "admit_durable_turn_lease"): "turn_admission",
    ("agent.relay_runtime", "acquire_conversation"): "relay_session",
    ("agent.conversation_loop", "_run_conversation_turn"): "conversation_setup",
    ("agent.turn_context", "build_turn_context"): "turn_context",
    ("agent.turn_context_compaction", "run_turn_start_compaction"): "context_preflight",
    ("agent.turn_context", "_collect_pre_llm_call_context"): "turn_context_hooks",
    ("agent.agent_runtime_helpers", "repair_message_sequence_with_cursor"): "history_validation",
    ("agent.turn_iteration_prep", "prepare_iteration"): "iteration_setup",
    ("agent.turn_iteration_prep", "begin_iteration"): "iteration_setup",
    ("agent.turn_iteration_prep", "announce_api_call"): "request_announcement",
    ("tools.env_probe", "get_environment_probe_line"): "environment_probe",
    ("agent.i18n", "_load_catalog"): "language_catalog",
    ("agent.turn_api_request", "build_api_request"): "request_hooks",
    ("agent.chat_completion_helpers", "interruptible_streaming_api_call"): "stream_dispatch",
}
_SERIALIZATION_PHASES = {
    ("httpx._content", "encode_json"): "request_serialization",
    ("json", "dumps"): "request_serialization",
    ("httpx2._content", "encode_json"): "request_serialization",
}


@contextmanager
def measure_preparation(timings, key, *, phases=_PREPARATION_PHASES, request_boundary=None):
    """Time selected calls on the current thread and keep an existing profiler intact."""
    if sys.getprofile() is not None:
        yield
        return
    span_stack = []
    totals = {}

    def finish_span(end):
        _, phase, start, children = span_stack.pop()
        duration = max(0.0, end - start)
        totals[phase] = totals.get(phase, 0.0) + max(0.0, duration - children)
        if span_stack:
            span_stack[-1][3] += duration

    def profile(frame, event, _arg):
        if request_boundary:
            # The HTTP worker can start sending while these caller frames are still open.
            while span_stack:
                finish_span(request_boundary[0])
            sys.setprofile(None)
            return
        if event == "call":
            phase = phases.get((frame.f_globals.get("__name__"), frame.f_code.co_name))
            if phase is not None:
                span_stack.append([frame, phase, perf_counter(), 0.0])
        elif event == "return" and span_stack and span_stack[-1][0] is frame:
            finish_span(perf_counter())

    sys.setprofile(profile)
    try:
        yield
    finally:
        if sys.getprofile() is profile:
            sys.setprofile(None)
        timings[key] = {f"{phase}_ms": round(seconds * 1000, 2) for phase, seconds in totals.items()}


@contextmanager
def measure_model_request(agent, timings):
    """Observe the existing HTTP and text stream without adding a text consumer."""
    started = perf_counter()
    lock = Lock()
    active = True
    timeline = {}
    requests = []
    request_boundary = []
    attached_clients = {}
    sdk_builders = {}
    sdk_builds = []
    create_client = agent._create_request_openai_client
    stream_hook = agent._enqueue_stream_hook
    timing_key = "google_meet_request_timing"

    def elapsed():
        return round((perf_counter() - started) * 1000, 2)

    def request_started(request):
        if request.method != "POST" or not request.url.path.rstrip("/").endswith(("/chat/completions", "/responses")):
            return
        with lock:
            if not active:
                return
            at = perf_counter()
            if not request_boundary:
                request_boundary.append(at)
            record = {"request_started_ms": round((at - started) * 1000, 2)}
            requests.append(record)
        request.extensions[timing_key] = record
        prior_trace = request.extensions.get("trace")

        def trace(event, info):
            if event.endswith(".send_request_body.complete"):
                with lock:
                    if active:
                        record.setdefault("request_sent_ms", elapsed())
            if prior_trace is not None:
                prior_trace(event, info)

        request.extensions["trace"] = trace

    def response_headers(response):
        record = response.request.extensions.get(timing_key)
        with lock:
            if active and record is not None:
                record["response_headers_ms"] = elapsed()
                record["status_code"] = response.status_code

    def observe_sdk_build(client, build):
        def timed_build(*args, **kwargs):
            started_at = perf_counter()
            record = {}
            try:
                with measure_preparation(record, "serialization", phases=_SERIALIZATION_PHASES):
                    return build(*args, **kwargs)
            finally:
                build_ms = round((perf_counter() - started_at) * 1000, 2)
                with lock:
                    if active:
                        sdk_builds.append({"sdk_request_build_ms": build_ms, **record.get("serialization", {})})

        with suppress(AttributeError, TypeError):
            client._build_request = timed_build
            sdk_builders[id(client)] = (client, build)

    def timed_client(*args, **kwargs):
        client = create_client(*args, **kwargs)
        with lock:
            if active and id(client) not in sdk_builders and callable(build := getattr(client, "_build_request", None)):
                observe_sdk_build(client, build)
        http = getattr(client, "_client", None)
        hooks = getattr(http, "event_hooks", None)
        if isinstance(hooks, dict):
            with lock:
                if active and id(http) not in attached_clients:
                    with suppress(AttributeError, TypeError, ValueError):
                        http.event_hooks = {
                            **hooks,
                            "request": [*hooks.get("request", ()), request_started],
                            "response": [*hooks.get("response", ()), response_headers],
                        }
                        attached_clients[id(http)] = http
        return client

    def timed_stream(event, *args, **kwargs):
        if event == "on_stream_delta" and kwargs.get("kind") == "text" and kwargs.get("delta", "").strip():
            with lock:
                if active and "first_visible_text_ms" not in timeline:
                    timeline["first_visible_text_ms"] = elapsed()
        return stream_hook(event, *args, **kwargs)

    def final_text_ready():
        with lock:
            timeline["final_text_ready_ms"] = elapsed()

    agent._create_request_openai_client = timed_client
    agent._enqueue_stream_hook = timed_stream
    try:
        with measure_preparation(timings, "request_preparation", request_boundary=request_boundary):
            yield final_text_ready
    finally:
        with lock:
            active = False
            preparation = timings.get("request_preparation")
            if preparation is not None and requests:
                # SDK build is measured separately and already overlaps stream dispatch.
                started_ms = requests[0]["request_started_ms"]
                preparation["unclassified_ms"] = round(max(0.0, started_ms - sum(preparation.values())), 2)
            timings["model_request_timing"] = {"http_requests": requests, "sdk_request_builds": sdk_builds, **timeline}
        agent._create_request_openai_client = create_client
        agent._enqueue_stream_hook = stream_hook
        for client, build in sdk_builders.values():
            with suppress(AttributeError, TypeError):
                client._build_request = build
        for http in attached_clients.values():
            with suppress(AttributeError, TypeError, ValueError):
                hooks = http.event_hooks
                http.event_hooks = {
                    **hooks,
                    "request": [hook for hook in hooks.get("request", ()) if hook is not request_started],
                    "response": [hook for hook in hooks.get("response", ()) if hook is not response_headers],
                }
