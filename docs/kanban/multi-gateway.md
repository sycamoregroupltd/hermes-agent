# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

## Single-dispatcher posture

Only one gateway owns the kanban dispatcher. The owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`. Dispatcher lock and ownership semantics are unchanged by the
notifier setting.

Notification delivery has a separate, optional gate:
`kanban.notify_in_gateway`. This matters in a profile-per-gateway deployment:
the sole dispatcher may not host the Telegram, Discord, webhook, or other
adapter that owns a subscription. Stamped subscriptions are routed through the
owning profile's live adapter and do not fall back to a different profile's
bot.

## Configuration

On the dispatch-owning gateway (typically the `default` profile), existing
configs need no change. On a non-owner gateway that must deliver subscriptions
for its hosted profile adapters, configure:

```yaml
kanban:
  dispatch_in_gateway: false
  notify_in_gateway: true
```

To run the dispatcher without a notifier, set the inverse explicitly:

```yaml
kanban:
  dispatch_in_gateway: true
  notify_in_gateway: false
```

Omitting `notify_in_gateway` (or setting it to `null`) preserves the historical
coupling: the notifier follows `dispatch_in_gateway`, including a false
`HERMES_KANBAN_DISPATCH_IN_GATEWAY` override. Once `notify_in_gateway` is an
explicit boolean, it is independent of that legacy dispatcher override.

## What each gateway does

| `dispatch_in_gateway` | `notify_in_gateway` | Dispatcher | Notifier |
|---|---|---|---|
| `false` | `false` | off | off |
| `false` | `true` | off | on |
| `true` | `false` | on | off |
| `true` | `true` | on | on |
| `false` | omitted / `null` | off | off (legacy inheritance) |
| `true` | omitted / `null` | on | on (legacy inheritance) |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.). Enable their notifier only when they need to poll
kanban subscriptions; every enabled notifier may open subscribed board DBs.
Atomic event claims prevent duplicate delivery, while profile-stamped routing
ensures a gateway cannot silently substitute another profile's adapter.
