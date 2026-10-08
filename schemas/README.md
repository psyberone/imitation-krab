# Krab JSON response schemas

`v1/` describes the stable response envelopes returned by the `/v1` API in
Krab 0.6.x. User-controlled prose is always under `untrusted_text`; identities,
routing, versions, timestamps, and sequence numbers are under
`trusted_metadata`.

| Response | Schema |
| --- | --- |
| Errors | `v1/error.schema.json` |
| `GET /v1/whoami` | `v1/whoami.schema.json` |
| `projects` | `v1/project-list.schema.json` |
| `sessions` / `session-create` | `v1/session-list.schema.json`, `v1/session.schema.json` |
| Single-claim mutations | `v1/claim.schema.json` |
| `claim-import` | `v1/claim-import.schema.json` |
| `issues` / `prs` | `v1/claim-list.schema.json` |
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

Source distributions retain this directory. Wheel installations place the
catalog under `$prefix/share/imitation-krab/schemas`.
