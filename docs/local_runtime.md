# Local Runtime

This describes the pre-server local-testing runtime: two long-running local
processes (poller + Telegram bot) that read and write the same SQLite
database. There is no VPS, no Cloudflare, no production scheduler yet — see
"Limitations before server deployment" below.

## Components

### Poller (`app/poller.py`, `python -m app.commands.run_poller`)

A single-threaded, sequential loop:

1. read `polling_enabled` from `system_state`
2. if paused, skip the Seoul SafeCity request entirely this cycle
3. if active, run one collect → dedup → send cycle (the same logic as
   `poll_once --send`)
4. run retention cleanup if `CLEANUP_INTERVAL_HOURS` has elapsed since the
   last cleanup
5. sleep until the next `POLL_INTERVAL_SECONDS` boundary (default **300s** —
   a new message may be detected up to ~5 minutes after it appears; or
   immediately, with a logged warning, if the cycle itself ran longer than
   the interval)
6. repeat until SIGINT/SIGTERM

Because each cycle fully completes before the next is even considered,
there is never more than one poll in flight — overlap is structurally
impossible in this design, not just discouraged.

### Telegram bot (`app/telegram_bot.py`, `python -m app.commands.run_telegram_bot`)

One long-polling process, one `getUpdates` offset sequence, handling
**both**:

- ordinary messages: authorizes every command against
  `TELEGRAM_ALLOWED_USER_IDS` (by Telegram user ID, never by chat ID
  alone), and replies to `/latest`, ordinary text (same as `/latest`),
  `/status`, `/pause`, `/resume`, `/help`, and an optional dev-only
  `/shutdown`
- `callback_query` updates: the entire Service v1 template
  selection/preview/confirm/cancel/AI flow, delegated to
  `app.template_flow.dispatch_callback` (see `docs/telegram_template_flow.md`)

It never initiates a Seoul SafeCity request itself — it only reads/writes
`system_state`, `messages`, and the template_* tables in the shared
database. There is exactly one bot process; nothing else calls
`getUpdates` for this bot token.

## Starting things locally

```bash
# One at a time (preferred — see "why separate processes" below):
python -m app.commands.run_poller
python -m app.commands.run_telegram_bot

# Or both together, in one terminal:
python -m app.commands.run_local
```

`run_local` starts both as child processes, logs each one's PID, and
monitors them; if either exits unexpectedly it shuts the other down too.

## Stopping locally

Press **Ctrl+C** in the terminal running the process (or send `SIGTERM`).
Each component:

- stops accepting new work immediately (poller: finishes not-yet-started;
  bot: exits its `getUpdates` loop)
- `run_telegram_bot` releases its single-instance lock file on exit
- `run_local` forwards the shutdown to both children, waits up to 10s for
  each to exit, then force-kills any that didn't

This is a clean, complete stop of the local processes — not the same thing
as pausing (see next section).

## `/pause` vs. stopping the bot

**`/pause` is the normal way to stop automatic collection and notifications
remotely.** Shutting down the Telegram bot process is not a substitute:

- if the bot process is stopped, it can no longer receive a remote
  `/resume` — you would need shell/SSH access to restart it
- `/pause` only stops the *poller's* automatic SafeCity requests and
  outbound alert notifications; `/status`, `/latest`, and `/resume` all
  keep working immediately, from anywhere, over Telegram
- the paused state is persisted in SQLite (`system_state.polling_enabled`)
  and survives a poller restart

An optional `/shutdown` exists only for local development convenience (see
below) — it is not the intended operational control.

## Telegram commands

| Command | Authorized only? | Effect |
|---|---|---|
| `/latest` | yes | Reply with the most recently collected record (full text) |
| *(any other text)* | yes | Same as `/latest` |
| `/status` | yes | Compact system status (see below) |
| `/pause` | yes | Stop automatic polling + notifications; idempotent |
| `/resume` | yes | Resume automatic polling + notifications; idempotent |
| `/help` | yes | List commands |
| `/shutdown` | yes, and only if enabled | Dev-only: stop this bot process |
| *(anything, unauthorized user)* | — | Generic denial; no data revealed |

`/status` never includes the bot token, chat ID, `.env` path, absolute
database path, or exception tracebacks — see `docs/telegram_setup.md` for
the exact format.

### `/shutdown` (development only)

Disabled unless **both**:

- `LOCAL_SHUTDOWN_COMMAND_ENABLED=true` in the environment, and
- the caller is in `TELEGRAM_ALLOWED_USER_IDS`

When disabled (the default), the bot replies that the command is disabled
and takes no action. This exists only so a developer can stop a local test
bot from their phone without shell access; it is not part of the normal
pause/resume operational flow and is not intended for any deployed
environment.

## Update mode: long polling (for now)

Local testing uses Telegram's `getUpdates` long polling
(`app/telegram_bot.py`), tracking one offset so no update — message or
callback_query alike — is processed twice, with a bounded backoff on
transient failures. Telegram itself rejects a second concurrent
`getUpdates` call for the same bot token with HTTP 409; `run_telegram_bot`
also takes a local file lock (`data/run_telegram_bot.lock`) so a second
local instance fails fast with a clear error instead of racing.

**Future migration (not implemented here):** a server deployment would
likely replace long polling with a Telegram webhook (Telegram pushes
updates to an HTTPS endpoint instead of the bot pulling them), which is
more efficient for an always-on server but requires a public HTTPS URL —
out of scope until VPS/Cloudflare deployment.

## SQLite concurrency model

The poller and Telegram bot are two separate local processes that may
touch the same SQLite file at the same time. `app/database.py` configures,
on every connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

WAL lets readers proceed without blocking on a writer; `busy_timeout` makes
SQLite block-and-retry internally for up to 5s on a write collision instead
of immediately raising "database is locked"; on top of that,
`Database._execute_write` retries a write up to 3 times with a short
backoff as a second safety net, raising `DatabaseLockedError` only if all
retries are exhausted. Every write is a single short statement immediately
followed by `commit()` — no write transaction is ever held open across an
HTTP request, a Telegram API call, or a sleep.

**Future migration (not implemented here):** moving beyond a single VPS
(e.g. edge/serverless deployment) would likely mean replacing local SQLite
with something like Cloudflare D1 or a managed PostgreSQL instance, since
SQLite's concurrency model assumes co-located processes on one filesystem.

## Limitations before server deployment

- No process supervision beyond `run_local`'s own child-process monitoring
  — no systemd/launchd unit, no auto-restart on crash, no VPS.
- No Cloudflare Worker, no cron, no public webhook.
- Long polling only; see the webhook migration note above.
- SQLite is a single local file; see the D1/PostgreSQL migration note above.
- `/shutdown` is a development convenience with no production role.
- On-demand AI slot extraction exists (`app/ai_client.py`,
  `AI_ENABLED`/`OPENAI_API_KEY`) but only runs on an explicit operator
  request — see `docs/service_v1.md`. No X posting, no automatic message
  classification — still entirely out of scope (see `app/future/`).
