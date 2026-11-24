"""Interactive authentication and lifecycle for a local Soar Meet participant."""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from hermes_cli.browser_runtime import chromium_executable
from utils import atomic_json_write

from .session import ORIGIN, meeting_url, read_json, request_identity, save_auth, workspace

_MODULE = "plugins.soar_meet.bot"


def register_cli(parser):
    commands = parser.add_subparsers(dest="soar_meet_command", required=True)
    commands.add_parser("setup")
    commands.add_parser("auth")
    join = commands.add_parser("join")
    join.add_argument("url")
    join.add_argument("--mode", choices=("transcribe", "realtime"), default="transcribe")
    join.add_argument("--wake", action="store_true", help='Answer questions addressed to "Hey Dev" or "Hey Horizon".')
    join.add_argument("--headed", action="store_true")
    join.add_argument("--wake-session", help="Reuse a dedicated Hermes chat with read-only FlowMind tools.")
    join.add_argument("--until", type=int, metavar="UNIX_TIMESTAMP",
                      help="Leave the scheduled call by this time.")
    join.add_argument("--start-call", action="store_true",
                      help="Start the scheduled call if the conversation is idle.")
    commands.add_parser("status")
    transcript = commands.add_parser("transcript")
    transcript.add_argument("--last", type=int, default=20)
    say = commands.add_parser("say")
    say.add_argument("text")
    commands.add_parser("stop")


def _root():
    root = workspace()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return root


def _alive(active):
    if not active:
        return False
    try:
        argv = Path(f"/proc/{int(active['pid'])}/cmdline").read_bytes().split(b"\0")
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return _MODULE.encode() in argv and os.fsencode(active["outDir"]) in argv


def _active():
    active = read_json(_root() / "active.json")
    if not active:
        raise ValueError("No Soar Meet run found.")
    return active


def _status():
    active = _active()
    alive = _alive(active)
    state = read_json(Path(active["outDir"]) / "status.json")
    return {"ok": True, **active, **state, "alive": alive,
            "inCall": bool(alive and state.get("inCall"))}


def _auth():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium", executable_path=chromium_executable(), headless=False)
        context = browser.new_context(locale="en-US")
        context.new_page().goto(ORIGIN, wait_until="domcontentloaded", timeout=30_000)
        print("Sign in to Soar Meet as the bot in Chromium, then return here and press Enter.", file=sys.stderr)
        input()
        user = save_auth(context)
        return {"ok": True, "signedInAs": user["name"], "authPath": str(workspace() / "auth.json")}


def _setup():
    import playwright.sync_api  # noqa: F401
    from plugins.google_meet.wake_caption import CaptionWake  # noqa: F401

    pulse = subprocess.run(["pactl", "info"], capture_output=True, timeout=5)
    auth = read_json(workspace() / "auth.json")
    return {"ok": pulse.returncode == 0, "pulseAudioReady": pulse.returncode == 0,
            "authSaved": bool(auth.get("storage_state")), "origin": ORIGIN}


def _check_scheduled_join(auth_path, mode):
    from playwright.sync_api import Error, sync_playwright

    auth = read_json(auth_path)
    storage_state = auth.get("storage_state")
    if not storage_state:
        raise ValueError("Sign in as the bot with soar-meet auth first.")
    try:
        with sync_playwright() as playwright:
            request = playwright.request.new_context(storage_state=storage_state)
            request_identity(request)
    except Error as exc:
        raise RuntimeError("Could not check the Soar Meet sign-in.") from exc
    if mode == "realtime":
        pulse = subprocess.run(["pactl", "info"], capture_output=True, timeout=5)
        if pulse.returncode:
            raise RuntimeError("PulseAudio is unavailable to the Hermes account.")


def _join(args):
    from agent.secret_scope import get_secret

    url = meeting_url(args.url)
    if args.wake and args.mode != "realtime":
        raise ValueError("--wake requires --mode realtime.")
    if args.wake_session:
        if not args.wake:
            raise ValueError("--wake-session requires --wake.")
        from plugins.google_meet.wake_chat import validate_wake_chat

        validate_wake_chat(args.wake_session)
    if args.until is not None and args.until <= time.time():
        raise ValueError("The scheduled call has already ended.")
    root = _root()
    auth_path = root / "auth.json"
    if not auth_path.is_file():
        raise ValueError("Sign in as the bot with soar-meet auth first.")
    if args.until is not None:
        _check_scheduled_join(auth_path, args.mode)
    env = os.environ.copy()
    if args.mode == "realtime":
        key = get_secret("HERMES_MEET_REALTIME_KEY") or get_secret("OPENAI_API_KEY")
        if not key:
            raise ValueError("Set HERMES_MEET_REALTIME_KEY in the Hermes profile.")
        env["HERMES_MEET_REALTIME_KEY"] = key
    with (root / "join.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _alive(read_json(root / "active.json")):
            raise ValueError("A Soar Meet bot is already active. Run soar-meet stop before another join.")
        out = Path(tempfile.mkdtemp(prefix="call-", dir=root))
        log = out / "bot.log"
        command = [sys.executable, "-P", "-m", _MODULE, str(out), url, args.mode]
        if args.headed:
            command.append("--headed")
        if args.wake:
            command.append("--wake")
        if args.wake_session:
            command.extend(("--wake-session", args.wake_session))
        if args.until is not None:
            command.extend(("--until", str(args.until)))
        if args.start_call:
            command.append("--start-call")
        with log.open("ab") as output:
            proc = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                env=env, start_new_session=True, close_fds=True,
            )
        active = {"pid": proc.pid, "outDir": str(out), "logPath": str(log), "mode": args.mode}
        if args.wake_session:
            active["wakeSessionId"] = args.wake_session
        try:
            atomic_json_write(root / "active.json", active)
        except BaseException:
            proc.terminate()
            raise
        return {"ok": True, **active}


def _stop():
    active = _active()
    if _alive(active):
        with contextlib.suppress(ProcessLookupError):
            os.kill(active["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while _alive(active) and time.monotonic() < deadline:
            time.sleep(0.2)
    status = _status()
    if status["alive"]:
        return {**status, "ok": False, "error": "The Soar Meet bot is still shutting down."}
    return status


def _transcript(last):
    path = Path(_active()["outDir"]) / "transcript.txt"
    lines = path.read_text().splitlines() if path.is_file() else []
    return {"ok": True, "total": len(lines), "lines": lines[-last:] if last > 0 else lines}


def _say(text):
    from plugins.google_meet.process_manager import append_say

    status = _status()
    if not (status["alive"] and status["inCall"] and status.get("realtimeReady") and text.strip()):
        raise ValueError("A nonempty reply and an active Realtime Soar Meet bot are required.")
    return {"ok": True, "enqueuedId": append_say(Path(status["outDir"]) / "say_queue.jsonl", text.strip())}


def dispatch(args):
    actions = {
        "setup": _setup, "auth": _auth, "join": lambda: _join(args), "status": _status,
        "transcript": lambda: _transcript(args.last), "say": lambda: _say(args.text), "stop": _stop,
    }
    old_mask = os.umask(0o077)
    try:
        result = actions[args.soar_meet_command]()
    except (EOFError, OSError, ValueError, ImportError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"ok": False, "error": str(exc)}
    finally:
        os.umask(old_mask)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1
