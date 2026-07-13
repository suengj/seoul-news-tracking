# Telegram Setup

Outbound: this project sends collected records to exactly one configured
Telegram chat (`TELEGRAM_CHAT_ID`). No approval workflow, no multi-chat
routing.

Inbound (local-runtime extension): a long-polling bot also accepts commands
from authorized users — see "Inbound commands" below. Still no approval
workflow, no X posting, no message rewriting.

## 1. Create a bot

1. In Telegram, message **@BotFather** → `/newbot`.
2. Follow the prompts to name the bot. BotFather returns a token that looks
   like `123456789:AAExampleTokenDoNotCommitThis`.
3. Put that token in `.env` as `TELEGRAM_BOT_TOKEN` (never commit `.env`).

## 2. Get the target chat ID

Pick one of:

- **Personal chat**: message the bot once (any text), then call
  `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser or via
  `curl` and read `message.chat.id` from the response.
- **Group chat**: add the bot to the group, send any message in the group,
  then call the same `getUpdates` endpoint — the group's `chat.id` is
  negative (e.g. `-1001234567890`).

Put that value in `.env` as `TELEGRAM_CHAT_ID`.

## 3. Enable sending

Set in `.env`:

```
TELEGRAM_SEND_ENABLED=true
```

Leaving this `false` (the default) makes every send path in this project a
no-op — records are still collected and stored, just never delivered. This
is intentional so collection can be exercised safely before delivery is
turned on.

## 4. `TELEGRAM_ALLOWED_USER_IDS`

A comma-separated list of Telegram **user IDs** (not usernames, not chat
IDs) allowed to use bot commands (`/latest`, `/status`, `/pause`,
`/resume`, `/help`, and ordinary text). Authorization is always checked
against the numeric `message.from.id` of the sender — never against the
chat ID, and never against a display name. Anyone not on this list gets a
generic denial reply with no data revealed.

To find your own numeric user ID: message any bot that echoes it (e.g.
`@userinfobot`), or read `message.from.id` from a `getUpdates` response
after messaging your own bot once.

This value is still a reserved placeholder for the future Telegram
*approve/reject* workflow (`app/future/approval_workflow.py`, not
implemented) — that is a separate, still-inactive feature from today's
inbound command handling.

## 5. Verify the connection

```bash
python -m app.commands.send_telegram_test --confirm
```

This requires both `TELEGRAM_SEND_ENABLED=true` in the environment and
`--confirm` on the command line — a safeguard against accidental sends. It
sends a clearly-labeled test message ("Seoul News Tracking - 연결 테스트"),
never a real disaster message, and prints the resulting Telegram message ID
on success.

## Outbound message format (new-alert notifications)

```
[서울안전누리 신규 재난문자]

발송지역/기관: {sender_or_region}
발송시각: {sent_at}

원문:
{complete_original_body}

출처:
{source_url}

수집시각:
{detected_at}
```

Dynamic fields are escaped for Telegram MarkdownV2; the original message
body is preserved exactly (no summarization, no symbol removal). Messages
longer than Telegram's 4096-character limit are split across multiple
`sendMessage` calls; all resulting message IDs are stored comma-joined in
`telegram_message_id`.

## Inbound commands (local runtime)

Start the bot with `python -m app.commands.run_telegram_bot` (see
`docs/local_runtime.md` for the full local-runtime picture, including
running it alongside the poller via `run_local`).

| Command | Reply |
|---|---|
| `/latest` | The most recently collected record — same layout as `/latest` below |
| *(any other text)* | Same as `/latest` |
| `/status` | Compact system status |
| `/pause` | Pauses automatic polling + notifications (idempotent) |
| `/resume` | Resumes automatic polling + notifications (idempotent) |
| `/help` | Lists commands |

`/latest` reply format:

```
[가장 최근 수집된 재난문자]

발송지역/기관: {sender_or_region}
발송시각: {sent_at}

원문:
{complete_original_body}

수집시각:
{detected_at}

데이터 상태:
{freshness relative to the last successful poll}

출처:
{source_url}
```

`/status` reply format (active):

```
[Seoul News Tracking 상태]

수집 상태: 실행 중
마지막 정상 수집: 2026-07-13 12:30:00 KST
최근 수집 이후: 1분
마지막 신규 문자: 2026-07-13 00:45:39 KST
저장된 문자: 123건
DB 보관기간: 90일
실행이력 보관기간: 14일
최근 오류: 없음
```

If the last successful poll is older than `STATUS_STALE_AFTER_MINUTES`
(default 5), `수집 상태` reads `점검 필요` instead of `실행 중`. If paused,
it reads `일시정지` along with who paused it and when. `/status` never
includes the bot token, chat ID, `.env` path, absolute database path, or
exception tracebacks (verified in
`tests/test_telegram_bot.py::test_status_does_not_leak_secrets_or_paths`).

Bot replies to inbound commands are sent regardless of
`TELEGRAM_SEND_ENABLED` — that flag only gates *automatic* new-alert
notifications, not a direct response to a user who just explicitly
messaged the bot. Both still require `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
(or rather, a valid bot token — inbound replies go to whichever chat the
message came from) to be configured.

There is also an optional, disabled-by-default `/shutdown` for local
development only (`LOCAL_SHUTDOWN_COMMAND_ENABLED=true` required) — see
"`/pause` vs. stopping the bot" in `docs/local_runtime.md` for why it is
not the normal way to stop automatic collection.
