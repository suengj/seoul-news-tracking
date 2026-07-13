# Telegram Setup

Part 1 sends collected records to exactly one Telegram chat. No approval
workflow, no multi-chat routing.

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

Reserved for the Part 2+ approval workflow (`app/future/approval_workflow.py`).
Part 1 only parses this value (comma-separated integers) to make sure the
config format is validated early; it has no effect on Part 1's outgoing-only
delivery. Safe to leave blank.

## 5. Verify the connection

```bash
python -m app.commands.send_telegram_test --confirm
```

This requires both `TELEGRAM_SEND_ENABLED=true` in the environment and
`--confirm` on the command line — a safeguard against accidental sends. It
sends a clearly-labeled test message ("Seoul News Tracking - 연결 테스트"),
never a real disaster message, and prints the resulting Telegram message ID
on success.

## Message format (Part 1)

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
