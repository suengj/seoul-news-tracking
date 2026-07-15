# Telegram routing validation

How to tell **cross-operator leakage** from **expected Telegram behavior** in
the v0.4.0 independent-operator model. See `docs/independent_operator_model.md`
for the model itself.

## Delivery cases

### Case A — independent private operators

Each authorized operator messages the bot in their own private chat. This is
the only supported operating mode.

- Interactive replies (`/latest`, `/history`, buttons, Preview, AI, `/status`,
  subscription commands, …) target only that operator's private `chat_id`.
- Automatic alerts fan out per subscription: operator A and operator B each
  receive their own `telegram_deliveries` row for a new message, delivered
  independently to their own chat.
- Operator A never receives operator B's interactive traffic, previews,
  decisions, or AI results, and vice versa.

If any of this crosses operators, it is a **routing bug**.

### Case B — group / supergroup / channel

Operational commands and buttons are **private-chat only**. A group action is
acknowledged (the spinner stops) and rejected in place with
`[개인 채팅에서 사용해 주세요] …` — it is never processed and never rerouted to a
private chat. There is no shared-group operating mode.

### Case C — automatic personal delivery

A genuinely new SafeCity message is fanned out by the poller to every active
personal subscription with `enforce_send_enabled=True`. Logs use
`delivery_mode=broadcast` for the send primitive, but the target is each
operator's private chat (a per-recipient `telegram_deliveries` row), never a
single `TELEGRAM_CHAT_ID`. One recipient's failure never blocks another;
retries target only the failed recipient. With zero active subscriptions the
message is stored and a WARNING is logged — there is **no** `TELEGRAM_CHAT_ID`
fallback.

## Structured logs

Each inbound action / outbound send emits:

```
Telegram route action=… delivery_mode=broadcast|interactive chat_type=…
outcome=… elapsed_ms=… routed_chat_id=… operator_user_id=… …
```

Use these fields to distinguish:

- operator A interactive vs operator B interactive (`operator_user_id`,
  `routed_chat_id`)
- automatic personal delivery arriving near the same time
- private vs group `chat_type` (and `outcome=group_rejected`)

Never log bot tokens, API keys, or full disaster bodies.

## Offline check

```bash
python -m app.commands.validate_telegram_behavior
```

Runs synthetic operators A (`201`/`200`), B (`202`/`300`) and a group chat
(`-400`) with fake transports — no Telegram / SafeCity / OpenAI network calls —
and prints an `Independent operator validation` PASS/FAIL block (non-zero exit
on failure) covering dual auto-register, fan-out, independent A-success/B-fail
retry, personal mute/subscribe, group rejection, preview/decision isolation and
coexistence, AI independence (including "B responds while A's AI is blocked"),
no legacy fallback, no backfill, and legacy decision migration.

## Live two-operator check

On merged `main`, restart the service and, with two real authorized operators
in separate private chats, confirm: both auto-receive a new alert
independently; each can run the template → preview → Final OK flow and AI
without affecting the other; a personal `/mute` silences only the muting
operator; a group action is rejected; and latency is acceptable. Tag `v0.4.0`
only after this passes.
