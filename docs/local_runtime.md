# Local Runtime

This describes the pre-server local-testing runtime: two long-running local
processes (poller + Telegram bot) that read and write the same SQLite
database. There is no VPS, no Cloudflare, no production scheduler yet — see
"Limitations before server deployment" below.

> **v0.4.0 — independent operators.** Automatic delivery is now personal:
> the poller fans each new record out to every active personal subscription
> (`telegram_deliveries`), not a single broadcast chat. `/pause` and `/resume`
> are personal `/mute`/`/unmute` aliases that silence only the calling
> operator — they no longer pause the shared poller. See
> `docs/independent_operator_model.md`.

## Components

### Poller (`app/poller.py`, `python -m app.commands.run_poller`)

A single-threaded, sequential loop:

1. read `polling_enabled` from `system_state`
2. if paused, skip the collection request entirely this cycle
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
  alone), enforces private-chat-only operation, auto-registers/touches the
  operator's personal subscription, and replies to `/latest`, `/history`,
  ordinary text (same as `/latest`), `/status`, `/subscribe`, `/unsubscribe`,
  `/mute`, `/unmute` (`/pause`/`/resume` aliases), `/help`, and an optional
  dev-only `/shutdown`
- `callback_query` updates: history selection plus the Service v1 template
  selection/preview/confirm/cancel/AI flow, delegated to
  `app.template_flow.dispatch_callback` (see `docs/telegram_template_flow.md`)

It never initiates a collection request itself — it only reads/writes
`system_state`, `messages`, and the template_* tables in the shared
database. There is exactly one bot process; nothing else calls
`getUpdates` for this bot token.

Before the first Telegram request, `run_telegram_bot` must claim the
two-host cutover authority described in
[`docs/systemd_deployment.md`](systemd_deployment.md). `CUTOVER_FENCE_PATH`
and `CUTOVER_HOST_ID` are mandatory for a real consumer; absent or invalid
authority refuses startup. The bot holds a fence-identity file lock for its
lifetime, derived from the token, fence path, and host id, so two local
processes with different database paths still contend for one lock. The
shared-filesystem requirements are documented in
[`docs/systemd_deployment.md`](systemd_deployment.md).

## Starting things locally

```bash
# One at a time (preferred — see "why separate processes" below):
python -m app.commands.run_poller
python -m app.commands.run_telegram_bot

# Or both together, in one terminal:
python -m app.commands.run_local
```

`run_local` starts both as child processes, logs each one's PID, and
monitors them. If one exits unexpectedly, only that one is restarted (with
backoff) — the sibling keeps running. A child that crashes repeatedly in a
short window is not retried forever: after enough crashes within the
rolling window, `run_local` gives up and exits nonzero rather than
crash-looping silently forever.

An optional outer layer (`scripts/launchd/com.user.seoulnews-runlocal.plist`,
macOS launchd, `KeepAlive`) restarts `run_local` itself if it exits for any
reason — including that give-up path, or any exception outside its own
child-supervision loop. Copy `scripts/launchd/com.user.seoulnews-runlocal.plist.example`
to a local-only `com.user.seoulnews-runlocal.plist` (gitignored), set your
project paths, then see the plist header comment for install/uninstall
commands. Without this outer layer, `run_local` giving up still leaves the
whole service down until a human restarts it manually.

For the Linux systemd deployment contract, use
[`docs/systemd_deployment.md`](systemd_deployment.md). It maps the observed
Mac `KeepAlive=true`, `RunAtLoad=true`, and `ThrottleInterval=60` to a
foreground systemd service with `Restart=always` and `RestartSec=60`. The Mac
plist remains a rollback reference; the Linux contract uses journald and an
external `EnvironmentFile`.

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

## Personal mute (`/pause`) vs. stopping the bot vs. shared polling

Since v0.4.0 `/pause` (alias of `/mute`) silences only the **calling
operator's** automatic delivery — it sets that operator's subscription
`muted` and does **not** touch the shared poller or any other operator. Use
`/resume` (alias of `/unmute`) to start receiving again.

- Muting one operator never stops the shared collection; other
  operators keep receiving alerts.
- `/status`, `/latest`, `/history`, `/resume` keep working while muted.
- Each operator's status is persisted in SQLite (`telegram_subscriptions`)
  and survives a restart.
- The shared collector (`system_state.polling_enabled`) is controlled only by
  the local `poller_control` command below — Telegram commands no longer drive
  that state.

### Local shared-collector control (`poller_control`, v0.4.1)

`poller_control` is the only explicit control over the shared
collector. It is a **local host command**, not a Telegram command, and is not
exposed via `/help`. `pause` here stops collection for **everyone** — it is
not a personal Telegram mute.

```bash
python -m app.commands.poller_control status   # read-only: polling_enabled,
                                                # last_successful_poll_at,
                                                # last_new_message_at,
                                                # last_poll_error, health
python -m app.commands.poller_control resume    # idempotently enable
python -m app.commands.poller_control pause      # idempotently pause (all)
```

`resume`/`pause` are idempotent and record a control event under the non-user
administrative actor id `0`. `status` prints no tokens and no Telegram
identifiers. This is the supported way to re-enable a collector that a
pre-v0.4.0 Telegram `/pause` left disabled (`polling_enabled = 0`) after
migrating an older database — a state where operators can be correctly
subscribed while collection is silently stopped.

An optional `/shutdown` exists only for local development convenience (see
below) — it is not the intended operational control.

## Telegram commands

| Command | Authorized only? | Effect |
|---|---|---|
| `/latest` | yes | Reply with the most recently collected record (full text) |
| `/history` | yes | Up to 10 recent records as selectable buttons |
| *(any other text)* | yes | Same as `/latest` |
| `/status` | yes | Personal alert status + shared collection status (see below) |
| `/subscribe` | yes | Start/re-activate personal automatic delivery to this chat |
| `/unsubscribe` | yes | Stop personal automatic delivery entirely |
| `/mute` (`/pause`) | yes | Personally silence automatic delivery; idempotent |
| `/unmute` (`/resume`) | yes | Resume personal automatic delivery; idempotent |
| `/help` | yes | List commands |
| `/shutdown` | yes, and only if enabled | Dev-only: stop this bot process |
| *(anything, unauthorized user)* | — | Generic denial; no data revealed |
| *(any command/button in a group)* | — | Rejected: private-chat only |

Every one of these is an **interactive reply**: it always returns to the
chat the request came from (`message.chat.id`), never to the legacy
`TELEGRAM_CHAT_ID`. All operational commands and buttons are private-chat
only — a group/supergroup/channel action is acknowledged and rejected in
place, never processed. See `docs/independent_operator_model.md` and
`docs/service_v1.md` "Broadcast vs. interactive delivery" for how to diagnose
a misrouted reply.

`/status` never includes the bot token, chat ID, `.env` path, absolute
database path, or exception tracebacks — and (v0.4.1) never the identity of
whoever paused the shared collector (`paused_by`/`resumed_by`). The shared
section may show the pause timestamp (`중지 시각`) without any actor id. See
`docs/telegram_setup.md` for the exact format. It does include the running
service version (see `docs/versioning.md`).

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
also takes a legacy database-directory lock plus the fence-identity lock, so
a second local instance fails fast with a clear error even when it uses a
different database path.

The offset is persisted in SQLite (`system_state.telegram_update_offset`),
not just kept in memory — a restart (clean or crashed) resumes from the
last persisted offset instead of replaying already-handled updates. See
`docs/telegram_template_flow.md` for the exact read/write points.

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

- Process supervision is local-machine only (`run_local`'s own child
  restart logic plus a platform supervisor around it). The repository now
  includes a source-only systemd contract for Linux and retains the launchd
  example for macOS; deployment authority still owns installation and service
  lifecycle.
- No Cloudflare Worker, no cron, no public webhook.
- Long polling only; see the webhook migration note above.
- SQLite is a single local file; see the D1/PostgreSQL migration note above.
- `/shutdown` is a development convenience with no production role.
- On-demand AI slot extraction exists (`app/ai_client.py`,
  `AI_ENABLED`/`OPENAI_API_KEY`) but only runs on an explicit operator
  request — see `docs/service_v1.md`. No X posting, no automatic message
  classification — still entirely out of scope (see `app/future/`).
