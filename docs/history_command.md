# `/history` command

Interactive selector for the **10 most recent** stored disaster messages.

## Behavior

- Authorized users only.
- Reply only to the inbound `chat_id` (`enforce_send_enabled=False`).
- Ordered by source `sent_at DESC`, then `internal_id DESC`.
- Excludes baseline (`is_baseline=0`) rows.
- Does **not** call Seoul SafeCity.
- Does **not** insert `template_suggestions`.
- Does **not** create Preview rows merely by listing.

## List UI

Message:

```
[최근 재난문자 N건]

원하는 발송시각을 선택해 주세요.
```

Buttons (one row each):

```
MM/DD HH:MM · {sender_or_region}
```

- KST timestamps
- region whitespace normalized; truncated for readability
- body text never appears on the button
- callback_data: `hist:{internal_id}` only

Empty DB → `저장된 재난문자가 없습니다.`

## Button click

1. Acknowledge callback immediately.
2. Load the exact row by `internal_id`.
3. Render via `send_initial_alert` into the **originating** chat
   (`persist_suggestion=False`, `enforce_send_enabled=False`).
4. Attach the normal category → template → Preview flow.

Missing/expired row → safe “기록을 찾을 수 없습니다” message; no fallback to
the newest record.

## Shared groups

If `/history` is used in a group, every member sees the reply. Prefer private
chats for per-operator isolation (see `docs/telegram_routing_validation.md`).
