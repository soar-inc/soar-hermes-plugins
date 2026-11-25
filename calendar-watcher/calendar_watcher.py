#!/usr/bin/env python3
"""Start the long-horizon Meet bot from a Google account's primary calendar."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SCOPE = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN = "https://oauth2.googleapis.com/token"
MEET = re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}/?")
SOAR_CHAT = re.compile(r"/chat/[A-Za-z0-9_-]{1,128}/?")
START_GRACE = timedelta(minutes=5)
CHAT_TITLE_ZONE = ZoneInfo("Asia/Kolkata")
CLI = [str(Path.home() / ".local/bin/hermes"), "-p", "long-horizon"]


def say(message):
    print(message, flush=True)


def request_json(request):
    with urlopen(request, timeout=15) as response:
        return json.load(response)


class Calendar:
    def __init__(self, credential_path):
        self.credentials = json.loads(credential_path.read_text())
        self.credential_key = hashlib.sha256(json.dumps(self.credentials, sort_keys=True).encode()).hexdigest()

    def access_token(self, db):
        if db is not None:
            cached = db.execute(
                "SELECT token FROM calendar_token WHERE credential_key = ? AND expires_at > ?",
                (self.credential_key, time.time()),
            ).fetchone()
            if cached:
                return cached[0]

        data = urlencode({**self.credentials, "grant_type": "refresh_token"}).encode()
        result = request_json(Request(TOKEN, data=data))
        token = result["access_token"]
        if db is not None:
            db.execute("DELETE FROM calendar_token")
            db.execute("INSERT INTO calendar_token VALUES (?, ?, ?)",
                       (self.credential_key, token, time.time() + max(0, result["expires_in"] - 60)))
            db.commit()
        return token

    def events(self, now, horizon, db=None):
        token = self.access_token(db)
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": (now - START_GRACE).isoformat(),
            "timeMax": (now + horizon).isoformat(),
            "fields": (
                "nextPageToken,items(id,summary,status,eventType,start(dateTime),end(dateTime),"
                "attendees(self,responseStatus),location,hangoutLink,conferenceData(entryPoints(entryPointType,uri))"
                ")"
            ),
        }
        while True:
            url = f"{EVENTS}?{urlencode(params)}"
            try:
                page = request_json(Request(url, headers={"Authorization": f"Bearer {token}"}))
            except HTTPError as exc:
                if exc.code != 401 or db is None:
                    raise
                db.execute("DELETE FROM calendar_token WHERE credential_key = ?", (self.credential_key,))
                db.commit()
                token = self.access_token(db)
                page = request_json(Request(url, headers={"Authorization": f"Bearer {token}"}))
            yield from page.get("items", ())
            if not page.get("nextPageToken"):
                return
            params["pageToken"] = page["nextPageToken"]


def event_times(event):
    if event.get("status") == "cancelled" or event.get("eventType", "default") != "default":
        return None
    if any(person.get("self") and person.get("responseStatus") == "declined"
           for person in event.get("attendees", ())):
        return None
    start = event.get("start", {}).get("dateTime")
    end = event.get("end", {}).get("dateTime")
    if not start or not end:
        return None
    return tuple(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end))


def meeting_plugin(link):
    if not isinstance(link, str):
        return None
    if MEET.fullmatch(link):
        return "meet"
    try:
        url = urlsplit(link)
    except ValueError:
        return None
    if url.scheme == "https" and url.netloc == "meet.soar.inc" and SOAR_CHAT.fullmatch(url.path):
        return "soar-meet"
    return None


def scheduled_meets(events, now, horizon):
    for event in events:
        times = event_times(event)
        if not times:
            continue
        start, end = times
        if not (now - START_GRACE <= start <= now + horizon and end > now):
            continue
        links = [event.get("hangoutLink", "")]
        links.extend(entry.get("uri", "") for entry in event.get("conferenceData", {}).get("entryPoints", ())
                     if entry.get("entryPointType") == "video")
        # An explicit Soar link in Location takes priority over a generated Google link.
        location = event.get("location")
        if meeting_plugin(location) == "soar-meet":
            links.insert(0, location)
        link = next((url for url in links if meeting_plugin(url)), None)
        if link and event.get("id"):
            yield event["id"], start, end, link, event.get("summary")


def meet_command(plugin, *args):
    run = subprocess.run([*CLI, plugin, *args], capture_output=True, text=True, timeout=45, check=False)
    try:
        result = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Hermes {plugin} {args[0]} returned invalid status (exit {run.returncode}).") from exc
    return result


def calendar_chat_title(summary, start):
    name = " ".join(summary.split()) if isinstance(summary, str) else ""
    date = start.astimezone(CHAT_TITLE_ZONE)
    return f"{(name or 'Calendar meeting')[:250]} · {date:%b} {date.day}, {date.year}"


def flowmind_chat_session(event_id, title, db):
    event_key = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    row = db.execute("SELECT generation FROM calendar_chat_generations WHERE event_key = ?", (event_key,)).fetchone()
    generation = row[0] if row else 0
    profile = Path.home() / ".hermes" / "profiles" / "long-horizon"
    result = subprocess.run(
        [CLI[0], "--run-module", "plugins.meeting_wake_chat", "--calendar-event-key", event_key,
         "--title-from-stdin", "--calendar-chat-generation", str(generation)],
        input=title, env={**os.environ, "HERMES_HOME": str(profile)}, capture_output=True, text=True,
        timeout=45, check=False,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Calendar chat setup failed. Check that the wake-chat patch is installed.") from exc
    if result.returncode or not data.get("ok"):
        raise RuntimeError(data.get("error") or "Calendar chat setup failed.")
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Calendar chat setup returned no session ID.")
    resolved_generation = data.get("generation")
    if type(resolved_generation) is not int or resolved_generation < generation:
        raise RuntimeError("Calendar chat setup returned no valid generation.")
    if resolved_generation > generation:
        db.execute(
            "INSERT INTO calendar_chat_generations (event_key, generation) VALUES (?, ?) "
            "ON CONFLICT(event_key) DO UPDATE SET generation = excluded.generation",
            (event_key, resolved_generation),
        )
        db.commit()
        say("Replaced a deleted Calendar chat and will use the fresh chat for this event.")
    if data.get("title_warning"):
        say(data["title_warning"])
    return session_id


def join_due(calendar, db, now, *, flowmind_chat=False):
    events = scheduled_meets(calendar.events(now, timedelta(seconds=1), db), now, timedelta(0))
    for event_id, start, end, link, summary in events:
        occurrence = (event_id, start.timestamp())
        end_timestamp = end.timestamp()
        attempted = db.execute(
            "SELECT 1 FROM joined_occurrences WHERE event_id = ? AND starts_at = ?", occurrence
        ).fetchone()
        # Older rows recorded only the end time. Keep their duplicate protection when
        # the end still matches, while allowing a reschedule that changed the end.
        legacy_attempt = db.execute(
            "SELECT 1 FROM joined WHERE event_id = ? AND ends_at = ?", (event_id, end_timestamp)
        ).fetchone()
        if attempted or legacy_attempt:
            say(f"Skipped meeting scheduled for {start.isoformat()}: join already attempted.")
            continue
        plugin = meeting_plugin(link)
        for candidate in ("meet", "soar-meet"):
            status = meet_command(candidate, "status")
            if status.get("alive"):
                say(f"Skipped meeting scheduled for {start.isoformat()}: Meet bot is already active.")
                return
            idle = status.get("reason") == "no active meeting" or status.get("error") == "No Soar Meet run found."
            if not status.get("ok") and not idle:
                raise RuntimeError(f"Could not check whether {candidate} is idle.")
        chat_args = []
        if flowmind_chat:
            chat_args = ["--wake-session", flowmind_chat_session(event_id, calendar_chat_title(summary, start), db)]
        remaining = (end - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            continue
        # Record before launching so a watcher restart cannot replay an ambiguous start.
        db.execute("INSERT INTO joined_occurrences (event_id, starts_at, ends_at) VALUES (?, ?, ?)",
                   (*occurrence, end_timestamp))
        db.commit()
        if plugin == "soar-meet":
            timing_args = ["--until", str(math.ceil(end_timestamp)), "--start-call"]
        else:
            timing_args = ["--duration", f"{math.ceil(remaining)}s"]
        result = meet_command(plugin, "join", link, "--mode", "realtime", "--wake", *timing_args, *chat_args)
        if not result.get("ok"):
            db.execute("DELETE FROM joined_occurrences WHERE event_id = ? AND starts_at = ?", occurrence)
            db.commit()
            detail = result.get("error") or result.get("reason") or "join rejected"
            raise RuntimeError(f"Hermes declined the scheduled {plugin} join: {detail}")
        say(f"Started meeting scheduled for {start.isoformat()} with bot PID {result.get('pid')}.")
        return


def authorize(client_file, credential_path, ssh_oauth_port=None):
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_file.is_file():
        raise RuntimeError(f"OAuth desktop client JSON was not found at {client_file}")
    if credential_path.exists():
        raise RuntimeError(f"Choose a new credential output path. File already exists at {credential_path}")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_file), [SCOPE], autogenerate_code_verifier=True,
    )
    if flow.client_type != "installed":
        raise RuntimeError("Use the OAuth client JSON for a Google Cloud Desktop app.")
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=ssh_oauth_port or 0,
        open_browser=ssh_oauth_port is None,
        timeout_seconds=300,
        prompt="consent",
    )
    if not credentials.refresh_token:
        raise RuntimeError("Google did not issue a refresh token.")
    with credential_path.open("x") as output:
        json.dump({name: getattr(credentials, name) for name in ("client_id", "client_secret", "refresh_token")}, output)
    say(f"Saved Calendar credentials to {credential_path}")


def check_once(calendar, credential_path, *, flowmind_chat=False):
    with (credential_path.parent / "watch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with sqlite3.connect(credential_path.parent / "joined.sqlite3") as db:
            db.execute("CREATE TABLE IF NOT EXISTS joined (event_id TEXT PRIMARY KEY, ends_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS joined_occurrences "
                       "(event_id TEXT NOT NULL, starts_at REAL NOT NULL, ends_at REAL NOT NULL, "
                       "PRIMARY KEY (event_id, starts_at))")
            db.execute("CREATE TABLE IF NOT EXISTS calendar_token "
                       "(credential_key TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS calendar_chat_generations "
                       "(event_key TEXT PRIMARY KEY, generation INTEGER NOT NULL)")
            now = datetime.now(timezone.utc)
            retain_from = (now - timedelta(days=1)).timestamp()
            db.execute("DELETE FROM joined WHERE ends_at < ?", (retain_from,))
            db.execute("DELETE FROM joined_occurrences WHERE ends_at < ?", (retain_from,))
            db.commit()
            join_due(calendar, db, now, flowmind_chat=flowmind_chat)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--authorize", type=Path, metavar="DESKTOP_CLIENT_JSON")
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--join-due", action="store_true", help="Check once and join one due meeting if idle.")
    parser.add_argument("--ssh-oauth-port", type=int, metavar="PORT",
                        help="Listen on a fixed loopback port for --authorize without opening a local browser.")
    parser.add_argument("--flowmind-chat", action="store_true",
                        help="Create or reuse a FlowMind chat for each Calendar event (with --join-due).")
    args = parser.parse_args()
    if args.flowmind_chat and not args.join_due:
        parser.error("--flowmind-chat requires --join-due.")
    if args.ssh_oauth_port is not None and (not args.authorize or not 1 <= args.ssh_oauth_port <= 65535):
        parser.error("--ssh-oauth-port requires --authorize and a port from 1 to 65535.")
    try:
        if args.authorize:
            authorize(args.authorize, args.credentials, args.ssh_oauth_port)
            return 0
        calendar = Calendar(args.credentials)
        if args.preview:
            now = datetime.now(timezone.utc)
            horizon = timedelta(days=1)
            meetings = list(scheduled_meets(calendar.events(now, horizon), now, horizon))
            for _, start, end, link, _ in meetings:
                label = "Soar Meet" if meeting_plugin(link) == "soar-meet" else "Google Meet"
                say(f"Eligible {label} from {start.isoformat()} to {end.isoformat()}")
            say(f"{len(meetings)} eligible meeting(s) in the next 24 hours.")
        else:
            check_once(calendar, args.credentials, flowmind_chat=args.flowmind_chat)
    except KeyboardInterrupt:
        say("Calendar watcher stopped.")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
        if isinstance(exc, RuntimeError):
            detail = str(exc)
        print(f"Calendar watcher failed ({detail}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
