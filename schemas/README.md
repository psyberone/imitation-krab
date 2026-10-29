# Krab JSON response schemas

`v1/` describes the stable response envelopes returned by the `/v1` API in
Krab 0.7. User-controlled prose is always under `untrusted_text`; identities,
routing, versions, timestamps, and sequence numbers are under
`trusted_metadata`. The `health --json` CLI liveness response is an exception:
it contains top-level `status` and `server_version` fields, with the latter
taken from the validated response header (or `null` for legacy servers).

| Response | Schema |
| --- | --- |
| Errors | `v1/error.schema.json` |
| `health --json` (CLI output, not the raw `GET /v1/health` body) | `v1/health.schema.json` |
| `GET /v1/whoami` | `v1/whoami.schema.json` |
| `projects` | `v1/project-list.schema.json` |
| `sessions` / `session-create` | `v1/session-list.schema.json`, `v1/session.schema.json` |
| Single-claim mutations | `v1/claim.schema.json` |
| `claim-import` | `v1/claim-import.schema.json` |
| `issues` / `prs` | `v1/claim-list.schema.json` |
| `claims --mine` | `v1/my-claims.schema.json` |
| `claim-show` | `v1/claim-detail.schema.json` |
| `send` / `show` / item status | `v1/item.schema.json` |
| `queue` / `inbox` | `v1/queue.schema.json` |
| Project activity | `v1/activity.schema.json` |
| `claim-review` | `v1/review-result.schema.json` |
| `inbox-ack` | `v1/inbox-ack.schema.json` |
| `admin user-list` | `v1/admin-user-list.schema.json` |
| `admin project-list` | `v1/admin-project-list.schema.json` |
| `admin project-members` | `v1/admin-project-members.schema.json` |

The schemas describe responses, not permissions. A syntactically valid response
does not authorize an operation, prove GitHub state, or make `untrusted_text`
safe to interpret as instructions.

Item, queue/inbox event, and activity event `trusted_metadata` require immutable
`completion_policy`, an enum of `workflow` and `receipt`. Workflow is the default
on creation and migration. Item status also includes `closed.handled`, exclusively
for receipt items. Receipt items can only be pending or handled; only their
recipient can handle them using the existing status endpoint with expected
version and an idempotency key. Claim-review notifications are receipt items.
Inbox acknowledgement remains independent of item completion.

Krab 0.7.0 item and private queue/inbox event metadata also require
`in_reply_to_item_id`, either null or an `itm_` identifier. Item creation accepts
the field only as a non-null item identifier when supplied. A bound target must
be a pre-existing item in the same project and session on the exact reverse
creator/recipient route. The link is immutable and does not affect either
item's lifecycle. Project activity events deliberately omit the field; exact
private conversation topology is not project-visible. This required nullable
response field is an additive wire change but a breaking change for strict
consumers of the closed v1 schemas.

Item creation accepts `completion_policy` as an optional string enum (`workflow`
or `receipt`); null and other values are rejected. The field cannot be updated.
Handling requests have `status: "closed.handled"`, a positive integer
`expected_version`, and an optional `note`, using the existing status route.
The `queue_limit` error's `details` object contains exactly `limit` (100) and
`open_items` (the route's open-item count); it contains no item or participant data.

Source distributions retain this directory. Wheel installations place the
catalog under `$prefix/share/imitation-krab/schemas`.
