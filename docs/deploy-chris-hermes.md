# Deploy on chris-hermes

Use the existing SSH/IAP access path for `chris-hermes`. Run the VM commands as the `hermes` account. The existing Hermes launcher, `long-horizon` profile and gateway should be available.

## 1. Install the code

Clone this repository into an approved workspace on the VM. From that repository:

```bash
integration_dir="$PWD"
hermes_repo="$HOME/.hermes/hermes-agent"
profile_dir="$HOME/.hermes/profiles/long-horizon"
watcher_dir="$profile_dir/calendar-watcher"
hermes_cli="$HOME/.local/bin/hermes"

test "$(git -C "$hermes_repo" rev-parse HEAD)" = 2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19
```

Continue once the check succeeds. Before an update, pause the watcher and stop any running test bot. Compare the plugin directories against the deployed copies and review any local changes:

```bash
for plugin in google_meet soar_meet; do
  if [ -d "$hermes_repo/plugins/$plugin" ]; then
    diff -qr --exclude=__pycache__ \
      "$hermes_repo/plugins/$plugin" "$integration_dir/plugins/$plugin"
  fi
done
```

Back up the existing plugin directories, shared module if present, and watcher files. Install both plugin directories, the shared wake-chat module and the two calendar files.

```bash
for plugin in google_meet soar_meet; do
  cp -R "$integration_dir/plugins/$plugin" "$hermes_repo/plugins/"
done

install -m 600 "$integration_dir/plugins/meeting_wake_chat.py" "$hermes_repo/plugins/meeting_wake_chat.py"
rm -f "$hermes_repo/plugins/google_meet/wake_chat.py"

install -d -m 700 "$profile_dir/calendar-watcher" "$profile_dir/scripts"
install -m 600 "$integration_dir/calendar-watcher/calendar_watcher.py"   "$profile_dir/calendar-watcher/calendar_watcher.py"
install -m 700 "$integration_dir/calendar-watcher/calendar-join.sh"   "$profile_dir/scripts/calendar-join.sh"
```

The launcher uses the `long-horizon` profile and the existing PulseAudio socket `/run/hermes-meet-audio/native`.

## 2. Enable and configure the plugins

```bash
"$hermes_cli" -p long-horizon plugins enable google_meet
"$hermes_cli" -p long-horizon plugins enable soar_meet
"$hermes_cli" -p long-horizon meet install
export PULSE_SERVER=unix:/run/hermes-meet-audio/native
pactl info
command -v paplay
"$hermes_cli" -p long-horizon meet join --help
"$hermes_cli" -p long-horizon soar-meet join --help
```

On this VM the PulseAudio socket is provided by `hermes-meet-audio.service`. An administrator can check it with `systemctl status hermes-meet-audio.service`. Use the VM's configured package management if Chromium prerequisites or PulseAudio tools are missing.

Configure the Realtime credential as `HERMES_MEET_REALTIME_KEY` in the profile's private `.env` through the approved secret process. The plugin can also use `OPENAI_API_KEY`. For event chats, the profile needs a working model provider, the default Compressor context engine, and an enabled MCP server named `flowmind` with the approved endpoint. The shared wake-chat code accepts an explicit read-only list drawn from `list_projects`, `list_tickets`, `count_tickets` and `get_ticket`, or `include: ["*"]` for all tools the server advertises to the profile. Set `resources: false` and `prompts: false` under `mcp_servers.flowmind.tools` for either option. The wildcard includes write tools available to the FlowMind credential. Participants can address Dev with a wake phrase during a call.

## 3. Authorize the three accounts or sessions

Existing working credentials can be reused. The following commands run as `hermes` when a session needs to be enrolled.

**Google Calendar.** Enable the Google Calendar API in the approved Cloud project and provision a Desktop app OAuth client. Place the client JSON on the VM in a private location. Use the calendar owner’s account and a loopback SSH tunnel to forward local port `8768` to `127.0.0.1:8768` on the VM. Add `-L 127.0.0.1:8768:127.0.0.1:8768` to the SSH/IAP connection from your workstation. Keep the tunnel open during authorization.

```bash
python3 -m venv "$watcher_dir/oauth-venv"
"$watcher_dir/oauth-venv/bin/python" -m pip install google-auth-oauthlib
"$watcher_dir/oauth-venv/bin/python" "$watcher_dir/calendar_watcher.py"   --authorize /path/to/private/oauth-client.json   --credentials "$watcher_dir/calendar-credentials.json"   --ssh-oauth-port 8768
```

Open the authorization URL on your workstation and select the account whose primary calendar the bot will watch. The output contains a refresh credential and is created with private permissions. To replace an existing authorization, use a new output filename, verify it with `--preview`, then move it into place.

**Google Meet and Soar Meet.** Run these in a graphical terminal on the VM, or use the VM's existing VNC session through SSH/IAP. Complete each sign-in in its own Chromium window, then press Enter in the corresponding terminal.

```bash
"$hermes_cli" -p long-horizon meet auth
"$hermes_cli" -p long-horizon soar-meet auth
```

Google Meet saves its browser state under `workspace/meetings/auth.json`. Soar Meet saves it under `workspace/soar-meet/auth.json`. The Soar account needs membership in the target conversation.

## 4. Verify the watcher before scheduling

```bash
/usr/bin/python3 "$watcher_dir/calendar_watcher.py"   --credentials "$watcher_dir/calendar-credentials.json" --preview
"$hermes_cli" -p long-horizon cron list
```

Preview reads the calendar without joining. Reuse **Long Horizon Calendar** if it is already present and points to `calendar-join.sh`. Resume it if you paused it for the update. If the profile has no watcher job, create one:

```bash
"$hermes_cli" -p long-horizon cron create 'every 1m'   --name 'Long Horizon Calendar' --script calendar-join.sh --no-agent --deliver local
```

## 5. Test the two providers

Use a test event on the watched calendar. Attach Google conferencing for a Google Meet test, or paste the full Soar conversation URL in **Location** for a Soar Meet test. Use a direct chat for a Soar outgoing-ring test, and start a channel call manually if the bot account has no Call control there. Enable transcription in Soar before testing a spoken wake question.

```bash
"$hermes_cli" -p long-horizon meet status
"$hermes_cli" -p long-horizon soar-meet status
```

Look for `inCall`, `realtimeReady` and `wakeEnabled`. Ask a read-only question such as “Hey Horizon, list my FlowMind projects.” Check that the reply is audible and that the Hermes chat in **Sessions** has the event's title and date. Job output is under `$profile_dir/cron/output/<job-id>/`. Use `meet stop` or `soar-meet stop` under the same profile to end a test bot run.

For a rollback, pause the watcher job and stop the bot, then restore the previous plugin, shared module, and watcher code from the deployment backup or the previous reviewed release. Preserve the profile's credential and meeting-data directories.
