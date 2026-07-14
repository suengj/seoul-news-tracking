# Telegram routing validation

How to tell **cross-chat leakage** from **expected Telegram behavior**.

## Three delivery cases

### Case A — separate private chats

Each authorized operator messages the bot in their own private chat.

- Interactive replies (`/latest`, `/history`, buttons, Preview, AI, `/status`,
  …) must target only that private `chat_id`.
- User A never receives User B’s private interactive traffic, and vice versa.

If this fails, it is a **routing bug**.

### Case B — same group / supergroup

Both operators use the bot inside one shared group. Telegram’s destination is
the **group** `chat_id`. Both members naturally see every reply.

This is **expected Telegram behavior**, not cross-user leakage. Private
per-user visibility requires each operator to use a private chat.

### Case C — automatic Broadcast

A genuinely new SafeCity message is sent only by the poller to
`TELEGRAM_CHAT_ID` with `enforce_send_enabled=True`. Logs use
`delivery_mode=broadcast`. If `TELEGRAM_CHAT_ID` is a shared group, both users
see the Broadcast regardless of who was clicking buttons at that moment.

## Structured logs

Each inbound action / outbound send emits:

```
Telegram route action=… delivery_mode=broadcast|interactive chat_type=…
outcome=… elapsed_ms=… routed_chat_id=… …
```

Use these fields to distinguish:

- User A interactive vs User B interactive
- automatic Broadcast arriving near the same time
- private vs group `chat_type`

Never log bot tokens, API keys, or full disaster bodies.

## Offline check

```bash
python -m app.commands.validate_telegram_behavior
```

Runs synthetic chats (Broadcast `100`, User A `200`, User B `300`) with fake
transports — no Telegram / SafeCity / OpenAI network calls.
