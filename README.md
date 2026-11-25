# Soar Hermes Meet plugins

This repository packages the Hermes-based meeting bot deployed on `chris-hermes` under the `long-horizon` profile. It includes the full Google Meet and Soar Meet plugin source from the VM and one calendar watcher for both providers.

| Path | Contents |
| --- | --- |
| [plugins/soar_meet](plugins/soar_meet) | Soar Meet browser participant, sign-in, transcripts and voice lifecycle |
| [plugins/google_meet](plugins/google_meet) | The deployed Google Meet plugin, including Realtime GA and captions |
| [plugins/meeting_wake_chat.py](plugins/meeting_wake_chat.py) | Event chat creation and reuse, FlowMind policy validation, and spoken replies for both Meet plugins |
| [tests/plugins](tests/plugins) | Mock-browser and wake-chat regression tests for the Hermes checkout |
| [calendar-watcher](calendar-watcher) | Google Calendar watcher and the Hermes cron launcher |
| [docs/deploy-chris-hermes.md](docs/deploy-chris-hermes.md) | Installation, credentials, scheduler and verification for the existing VM |

Both plugins run on [Hermes Agent commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`](https://github.com/NousResearch/hermes-agent/commit/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19) (v0.23.0). Install both plugin directories and the shared wake-chat module together. The upstream Hermes license is included in [LICENSE.hermes-agent](LICENSE.hermes-agent).

## How the calendar flow works

- The cron job checks one authorized Google Calendar. It recognizes standard Google Meet conferencing and Soar Meet chat links in event **Location**. A Soar link in Location takes priority if both links are present.
- The watcher uses the event ID and start time to avoid duplicate joins. The event ID also identifies its Hermes chat. Rescheduling keeps the chat and updates its title. If the chat was deleted, a fresh one is created at the next eligible join.
- With captions available, the bot can answer spoken wake questions in the call using the configured FlowMind tools. Soar recognizes **Hey Dev** and **Hey Horizon**. Google Meet wake phrases are configured in the Hermes profile.
- Both bots use the scheduled end time or duration and can leave after being alone for 15 seconds once another participant has joined. A scheduled Soar call that the bot starts has a ten-minute wait for the first participant.

Browser sign-in sessions, OAuth credential files, profile secrets, databases, and meeting artifacts belong outside this repository.
