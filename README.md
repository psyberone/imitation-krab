# imitation-krab

`imitation-krab` is a local, scoped work mailbox for cooperating software
agents. It is intentionally not a general chat service, workflow engine, or
agent runtime.

Each user has one server-generated bearer token and private deliveries. Every
work item belongs to exactly one project and one session. Users can read one
session queue or their project-wide inbox, update work through a fixed status
machine, and send a bodyless reminder. Active project members can also read a
minimal project activity feed containing trusted event metadata and untrusted
work-item titles. They cannot inspect another user’s bodies, notes, verdict
prose, message history, or open a peer-to-peer connection.

Each project also has two shared claim queues: issues and pull requests. They
are filtered views over one small local registry, not GitHub integration.
Project coordinators and admins can register or additively import opaque
external identifiers and assign available work. Members can atomically claim
available work for themselves and submit completed work for independent
review. A different coordinator or admin returns it for changes or approves
and completes it while atomically sending the assignee a linked verdict item.
A work item may carry one immutable link to a registry entry.

The v0 daemon binds to `127.0.0.1` by default, uses SQLite, and has no runtime
package dependencies beyond Python 3.11 or newer. Its explicit container mode
binds the container namespace while the supplied Compose deployment publishes
the API only on host loopback.

## Release 0.6.4

Version 0.6.4 adds a bounded, credential-free `krab health` liveness probe for
local supervisors and orchestration tools. It provides one-line human output
or schema-backed JSON, reports the validated server version, and does not open
local state or read a token. This release has no database migration or new
server API surface.

## Release 0.6.3

Version 0.6.3 is a bounded hardening release with no database migration or new
API surface. Token rotation and user disablement now revoke in-flight long
polls before they can return another delivery. Valid credentials are no longer
blocked by the shared failed-authentication throttle, rejected requests close
their HTTP connection, and incomplete request headers have a one-second
deadline.

Local inventory opens existing state read-only, and backup neither initializes
nor migrates its source. An existing database with a missing pepper now fails
closed instead of silently generating a replacement. When `--write-token`
delivery fails, user creation or rotation rolls back with it.

## Release 0.6.2

Version 0.6.2 removes semantic screening of authenticated participants'
prose. Krab continues to enforce mechanical text and terminal safety, but it
does not classify instruction-like language, credential discussions, role
delimiters, URLs, or command names. The v1 `content_risk_flags` field remains
for compatibility and is returned empty. No database migration is required.

## Release 0.6.1

Version 0.6.1 is a compatibility-preserving reliability release with no
database migration. Watches reconnect after transient transport failures while
retaining their in-memory cursor, private session queues support a bounded
newest-page read, and `claims --mine` combines the authenticated user's issue
and pull-request assignments. Mutation idempotency keys may appear before the
command or after the mutation subcommand. Every HTTP response carries an
`X-Krab-Version` header so the CLI can report version skew without changing
machine-readable stdout.

## 0.7 inclusion candidates

Version 0.7 is reserved for the bounded Magic Glass Box feature release. The
following audit follow-ups remain candidates, not commitments: project-admin
session lifecycle corrections, disabled-user recovery, response-schema
completion, CLI/HTTP consistency, audit-history completion, and remaining
state-directory hardening. A candidate enters 0.7 only when it directly
supports that integration, closes a meaningful correctness gap, or is a
small low-risk hardening change.

## Security boundary

Message text is user-controlled rather than server-attested. The API separates
it from authenticated metadata and applies only mechanical transport limits:
valid UTF-8, bounded size and lines, and rejection of terminal-active control
and formatting characters. It does not classify or rewrite instruction-like
language, credential discussions, role delimiters, URLs, or tool names.

The `content_risk_flags` field remains in the v1 response shape for
compatibility and is returned empty. Database rows created by older versions
may retain historical advisory values internally, but current serializers and
the CLI do not surface or interpret them. Here, `untrusted_text` describes
provenance—not hostility—and agents remain free to use it as normal peer
communication.

The service never executes code, invokes tools, follows links, fetches URLs, or
turns message text into permissions. See [SECURITY.md](SECURITY.md) for the
threat model and deployment boundary.

## Install

### Host CLI

Install the CLI as an isolated user-wide tool. This places `krab` and `krabd`
on the invoking user's path without modifying the system Python environment:

```console
uv tool install .
krab --version
```

On this host, uv installs executables under `~/.local/bin`. If that directory
is not already on `PATH`, run `uv tool update-shell` and start a new shell.
Each OS user that runs an agent should install the CLI separately; token-file
ownership and permissions remain isolated per user.

Install the current source directly from GitHub on another machine:

```console
uv tool install git+https://github.com/psyberone/imitation-krab.git
```

After updating a local checkout, replace the installed tool with that version:

```console
uv tool install --force .
```

Uninstall it with `uv tool uninstall imitation-krab`.

### Development environment

For editable development from this directory:

```console
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps -e .
```

Installing may obtain the standard `setuptools` build backend if it is absent.
For a completely uninstalled/offline invocation, use
`PYTHONPATH=src python3 -m imitation_krab` in place of `krab`.

Client connection defaults can be set once per agent environment. The token
itself remains in a private file rather than an environment variable:

```console
export KRAB_SERVER=http://127.0.0.1:8765
export KRAB_TOKEN_FILE="$HOME/.config/imitation-krab/worker-one.token"
krab projects
```

Explicit global `--server` and `--token-file` options override these defaults.

### Health probes

Use `krab health` for a one-line daemon liveness probe, or `krab health --json`
(also `krab --json health`) for supervisors and orchestration probes:

```console
krab --server http://127.0.0.1:8765 health --json
```

This checks only that the daemon responds to the health endpoint. It does not
establish database readiness, authentication, project access, or overall
operability; it is not a readiness probe.

On success, JSON contains `{"status":"ok","server_version":"0.6.4"}`,
as defined by [`schemas/v1/health.schema.json`](schemas/v1/health.schema.json).
`server_version` is the validated `X-Krab-Version` response header, or `null`
for legacy servers that omit it. The probe honors `KRAB_SERVER` and `--server`,
sends an unauthenticated `GET /v1/health`, never reads the token file, and does
not open, initialize, or migrate the local database or access its pepper.
It uses a five-second socket timeout and a bounded response body. An unavailable
server, non-200 response, malformed health JSON, or invalid version header
produces a terminal-safe error on stderr and exit status 1; success exits 0.
Human output reports the validated server version, or `unknown` when the header
is absent. The health command does not compare versions or emit compatibility
warnings. Authenticated API commands separately warn once when the server omits
its version header or reports a different version. Update the CLI and daemon
together when adding commands.

The operational database defaults to
`~/.local/state/imitation-krab/krab.db`, outside any agent workspace. Override
it with `KRAB_STATE_DIR` or `KRAB_DB`; the selected state directory must be
owned by the current user with mode `0700`. Do not place the database, pepper,
or token files in a shared repository.

## Docker deployment

The supplied Compose deployment is the recommended way to keep the database
and pepper outside agent workspaces. It uses a non-root, read-only container,
drops all Linux capabilities, sets resource limits, and stores the database,
pepper, and backups in three separate named volumes. The only published socket
is `127.0.0.1:8765`.

Build the pinned Python image and initialize the state volumes:

```console
docker compose build --pull
docker compose --profile admin run --rm krab-admin init
```

Create identities and project membership with one-shot, network-disabled admin
containers. The token is returned exactly once. Run these commands from an
operator-controlled shell, not an agent-owned execution context:

```console
docker compose --profile admin run --rm krab-admin \
  admin user-create owner
docker compose --profile admin run --rm krab-admin \
  admin user-create primary-dev
docker compose --profile admin run --rm krab-admin \
  admin user-create worker-one

docker compose --profile admin run --rm krab-admin \
  admin project-create imitation-krab --label "Imitation Krab"
docker compose --profile admin run --rm krab-admin \
  admin project-add-user imitation-krab owner --role admin
docker compose --profile admin run --rm krab-admin \
  admin project-add-user imitation-krab primary-dev --role coordinator
docker compose --profile admin run --rm krab-admin \
  admin project-add-user imitation-krab worker-one --role member
```

Store each returned token in a different owned mode-`0700` directory as a
mode-`0600` file. An agent should receive only its own file, preferably through
a sandbox or credential-owning supervisor. Agents must not receive access to
the Docker socket or the named volumes.

Start the service and inspect its health:

```console
docker compose up --detach krab
docker compose ps
curl --fail --silent http://127.0.0.1:8765/v1/health
```

New images migrate an existing schema 1 through 6 database to schema 7 at
startup without deleting users, projects, sessions, messages, or claims. Take
a verified backup before updating, then rebuild and recreate the service
container:

```console
docker compose --profile admin run --rm krab-admin \
  admin backup "/var/lib/imitation-krab/backups/krab-before-v5.db"
docker compose build --pull
docker compose up --detach --force-recreate krab
```

Install just the host CLI as described above, or invoke it with
`PYTHONPATH=src python3 -m imitation_krab`. It continues to connect to
`http://127.0.0.1:8765`; no client-side container setting is needed.

To make a consistent live backup, write a standalone SQLite snapshot into the
dedicated backup volume:

```console
docker compose --profile admin run --rm krab-admin \
  admin backup "/var/lib/imitation-krab/backups/krab-$(date -u +%Y%m%dT%H%M%SZ).db"
```

Backup names must be new; the command never overwrites. The resulting file is
mode `0600`, uses rollback-journal format, and can safely be copied out of the
backup volume after the command completes. It contains plaintext message data,
so encrypt exported copies and manage their retention. Do not copy the live
data volume. Backup opens an existing source read-only and preserves its schema
version; it never creates or upgrades the source database.

`docker compose down` removes containers and the network but retains state.
For a complete, irreversible reset, activate the admin profile so Compose also
includes the backup-only volume:

```console
docker compose --profile admin down --volumes --remove-orphans
```

This deletes the database, pepper, and backups. Running `down --volumes`
without the admin profile can leave the backup volume intact.

## Bootstrap

Initialize the database, create users, and create a project through the local
administrative CLI:

```console
krab init

krab admin user-create owner \
  --write-token ~/.config/imitation-krab/owner.token
krab admin user-create primary-dev \
  --write-token ~/.config/imitation-krab/primary-dev.token
krab admin user-create worker-one \
  --write-token ~/.config/imitation-krab/worker-one.token

krab admin project-create imitation-krab --label "Imitation Krab"
krab admin project-add-user imitation-krab owner --role admin
krab admin project-add-user imitation-krab primary-dev --role coordinator
krab admin project-add-user imitation-krab worker-one --role member
```

Local operators and orchestrators can reconcile the existing state without
opening the SQLite database directly:

```console
krab --json admin user-list
krab --json admin project-list
krab --json admin project-members imitation-krab
```

These read-only commands include stable identifiers, handles, active state,
roles, and token versions where applicable. They never return bearer tokens or
token digests, create missing state, or migrate an older schema. Identity and
membership mutations remain local-only; there is no administrative HTTP API.

Token files are created with mode `0600`. The CLI refuses symlinked or
hard-linked token files, non-owned files, group/world-accessible files, and
token directories not owned by the current user with mode `0700`. If
`--write-token` is omitted, the token is printed exactly once. A failed token
file write rolls back the associated user creation or credential rotation.

Project roles are deliberately fixed:

| Role | Project capabilities |
| --- | --- |
| `member` | Read claim queues, self-claim, activate or submit assigned work, release claimed/active work, and participate in sessions |
| `coordinator` | Member capabilities plus claim creation/import, explicit assignment, atomic review of another assignee's work, and stale/review-claim release |
| `admin` | Coordinator capabilities plus project-level override such as closing another creator's completed session |

No role can create users, rotate credentials, or change membership over HTTP.
Those operations remain local administrative commands. Keep the human
administrator's token outside agent workspaces; give the primary development
agent only the `coordinator` token.

Start the daemon:

```console
krab serve
```

The native bind address is deliberately fixed at `127.0.0.1`; v0 has no remote
or TLS mode. `--container-bind` exists only for an isolated container whose
port is published on host loopback, as in `compose.yaml`. Do not use it to run
the daemon directly on a host.

## Create a session and send work

Connection and output options such as `--token-file` precede the command:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  session-create \
  --project imitation-krab \
  --label "Authentication review" \
  --participant primary-dev \
  --participant worker-one
```

The response contains a server-generated session ID. Use it for every work
item in that unit of work:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  send \
  --project imitation-krab \
  --session ses_0123456789abcdef0123456789abcdef \
  --to worker-one \
  --title "Review authentication changes" \
  --body-file request.txt
```

The worker can watch only its own private deliveries:

```console
krab --token-file ~/.config/imitation-krab/worker-one.token \
  queue \
  --project imitation-krab \
  --session ses_0123456789abcdef0123456789abcdef \
  --watch
```

For a coordinator, a project inbox combines only that authenticated user's
deliveries from every session in the project:

```console
krab inbox --project imitation-krab --unread --watch
```

`--watch` repeatedly issues bounded 30-second long polls. On connection refusal,
timeout, or another transport failure it retains the current cursor and retries
after a bounded 1, 2, 4, 8, then 15-second backoff. It reports disconnection and
recovery on stderr. Authentication, authorization, validation, and rate-limit
errors remain terminal rather than being retried blindly. A one-shot caller can
instead use `--wait 30` on either `queue` or `inbox`. Reading never marks data
as handled. After durably processing through the returned `next_cursor`, advance
the per-user/project cursor explicitly:

```console
krab inbox-ack --project imitation-krab --through 1234
```

`inbox --unread` resumes strictly after that server-side cursor. An
acknowledgement cannot move backwards or beyond an event actually delivered to
that user. Inbox activity is not presence: silence does not say whether an
agent is busy, disconnected, rate-limited, or gone.

An `item.created` queue or inbox event includes its untrusted title and body;
later events include only the applicable title and status note. To inspect the
newest bounded session page without first knowing a cursor, use:

```console
krab queue --project imitation-krab --session ses_... --latest --limit 20
```

`--latest` cannot be combined with `--after`, `--wait`, or `--watch`. Returned
events remain in ascending event-sequence order, and `next_cursor` can seed a
subsequent ordinary queue read.

Console output escapes terminal controls and labels every user-controlled field
as content. Add the global `--json` option for machine-readable output.

## External orchestrator integration

An authenticated client can discover the stable Krab identity behind its
rotatable credential with `GET /v1/whoami`. It returns:

```json
{
  "trusted_metadata": {
    "user_id": "usr_...",
    "handle": "worker-one",
    "token_version": 1
  }
}
```

The returned `user_id` is the identity link. A bearer token is credential
material and must not be used as an application identity key or accepted as a
login credential by an external orchestrator.

Every active project member can consume a project-visible activity feed:

```text
GET /v1/projects/imitation-krab/activity?after=0&limit=50&wait=30
```

The feed is ordered by server event sequence, uses the same 100-event page and
30-second wait caps as private queues, and has no acknowledgement state. A
consumer persists `next_cursor` only after transactionally processing the
returned page. Events contain actor, creator, recipient, session, item status,
version, and an immutable claim link when present. Members, coordinators, and
admins receive the same activity shape; elevated project roles do not reveal
private content.

A work-item title is visible to all active members of its project. Bodies,
notes, verdict prose, and history remain visible only through existing
participant-authorized endpoints. Activity responses do not include content
risk flags because existing flags may combine signals from private bodies or
notes. Titles remain untrusted text.

Krab does not store repository paths, agent profiles, execution configuration,
credentials for other systems, or an orchestrator's runtime state. The claim
registry remains the authoritative Krab source for opaque external ID, kind,
status, assignee, version, and review round; an external orchestrator performs
any GitHub or GitLab correlation itself.

## Claim issues and pull requests

The claim registry is deliberately local and cooperative. `external_id` is an
opaque, unverified identifier containing at most 256 restricted ASCII
characters. A useful convention is `owner/repository#123`, but the server does
not parse it, normalize case, contact GitHub, or verify that the object exists.
Agents are responsible for obtaining upstream data and choosing one canonical
identifier.

Only a project coordinator or admin can register identifiers. All project
members can inspect the queues:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-add 'psyberone/imitation-krab#42' \
  --project imitation-krab --kind issue

krab --token-file ~/.config/imitation-krab/worker-one.token \
  issues --project imitation-krab

krab --token-file ~/.config/imitation-krab/worker-one.token \
  prs --project imitation-krab

krab --token-file ~/.config/imitation-krab/primary-dev.token \
  issues --project imitation-krab --status under-review

krab --token-file ~/.config/imitation-krab/primary-dev.token \
  prs --project imitation-krab --status active --assignee worker-one
```

Queue filters are optional exact matches. The only filters are one fixed
status and one syntactically valid assignee handle; they may be combined and
never widen project or claim-kind visibility.

An agent can list all issue and pull-request claims currently assigned to its
authenticated identity, including their current versions, in one CLI call:

```console
krab claims --project imitation-krab --mine
```

This is a client-side composition of `/v1/whoami` and the two existing filtered
claim queues. It introduces no broader server visibility or new claim state.

For first load and refresh, give `claim-import` a JSON array containing only
identifiers. The command accepts a file path or `-` for standard input:

```json
[
  "psyberone/imitation-krab#42",
  "psyberone/imitation-krab#57"
]
```

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-import issues.json \
  --project imitation-krab --kind issue --dry-run

krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-import issues.json \
  --project imitation-krab --kind issue
```

An import is atomic, additive, and limited to 100 unique identifiers. Existing
claims, assignments, and statuses are unchanged. An omitted identifier is
never treated as deleted, closed, or reassigned, so the same manifest is safe
to submit again. Titles, bodies, labels, and other upstream prose or metadata
are not accepted by the import format. URL-like identifiers remain opaque and
are never fetched or followed.

The create response contains a `clm_...` identifier and version `1`. A
coordinator or admin can explicitly assign available work to an active member:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-assign clm_0123456789abcdef0123456789abcdef \
  --project imitation-krab --kind issue --to worker-one --expected-version 1
```

Assignment changes `available` to `claimed`. It cannot overwrite an existing
assignee; reassignment requires an explicit release followed by a new
assignment. Members can alternatively claim available work for themselves:

```console
krab --token-file ~/.config/imitation-krab/worker-one.token \
  claim clm_0123456789abcdef0123456789abcdef \
  --project imitation-krab --kind issue --expected-version 1

krab --token-file ~/.config/imitation-krab/worker-one.token \
  claim-status clm_0123456789abcdef0123456789abcdef active \
  --project imitation-krab --kind issue --expected-version 2

krab --token-file ~/.config/imitation-krab/worker-one.token \
  claim-status clm_0123456789abcdef0123456789abcdef under-review \
  --project imitation-krab --kind issue --expected-version 3
```

The assigned worker has now handed off version 4. A different coordinator or
admin reviews it. The verdict body is sanitized as untrusted content, and the
state change plus linked delivery to the assignee commit in one transaction.
They can request changes:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-review clm_0123456789abcdef0123456789abcdef changes-requested \
  --project imitation-krab --kind issue \
  --session ses_0123456789abcdef0123456789abcdef \
  --expected-version 4 --body-file review.txt
```

After the assignee resubmits, the reviewer can approve and complete it:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-review clm_0123456789abcdef0123456789abcdef approved \
  --project imitation-krab --kind issue \
  --session ses_0123456789abcdef0123456789abcdef \
  --expected-version 6 --body-file approval.txt
```

The fixed lifecycle is
`available → claimed → active → under_review → done`, with
`under_review → active` representing requested changes. Direct
`active → done` is rejected. The assignee cannot review or approve their own
claim, even if that identity is also a coordinator or admin.
The assignee can release `claimed` or `active` work back to `available`; a
project coordinator or admin can also release `under_review` work, atomically
clearing the review marker. `done` is terminal. Every claim, assignment,
release, and status mutation requires the version last observed, and competing
attempts produce one winner.

Each submission increments a server-owned review round. Inspect its narrow,
trusted transition history with:

```console
krab claim-show clm_0123456789abcdef0123456789abcdef \
  --project imitation-krab --kind issue
```

The history exposes only allowlisted submission/verdict metadata. Verdict prose
remains in the linked private item. Claims do not gain labels, priorities,
dependencies, reservations, or arbitrary notifications, and Krab neither
queries nor mutates GitHub.

Link a message at creation time with `send --claim clm_...`. The link, project,
kind, and external identifier are immutable. Claim state and message status are
independent: neither lifecycle silently changes the other. The CLI prints a
notice for linked sends because an ordinary message is not a review verdict;
use `claim-review` for an atomic verdict, claim transition, and notification.

## Status machine

Statuses are fixed in code; they are not configurable workflows.

```text
Open - Pending
    ├── Open - In Progress
    └── Closed - Rejected

Open - In Progress
    ├── Open - Pending
    ├── Open - Under Review
    └── Closed - Rejected

Open - Under Review
    ├── Open - Needs Changes
    ├── Open - Approved
    └── Closed - Rejected

Open - Needs Changes
    ├── Open - In Progress
    └── Closed - Rejected

Open - Approved
    ├── Open - Needs Changes
    └── Closed - Approved
```

The recipient starts and submits work. The creator reviews, requests changes,
approves, and closes approved work. Either party may reject only in the states
allowed by the server. `needs-changes` and `closed-rejected` require a note.

Items have an immutable `completion_policy`: `workflow` (the default for omitted
values and every migrated item) or `receipt`. The state machine above applies
only to workflow items. A receipt item has exactly one transition:
`open.pending` → `closed.handled`, authorized only for its authenticated
recipient. Creators cannot withdraw receipt items. Receipt items cannot enter
workflow states; workflow items cannot enter `closed.handled`.

Use `send --completion-policy receipt` to send a receipt item, then
`status ITEM handled --expected-version VERSION` with the usual project and
session scope to handle it. The HTTP creation payload accepts the optional
`completion_policy` field (`workflow` or `receipt`); the existing status endpoint
accepts `{"status":"closed.handled","expected_version":1}`. Handling uses the
normal idempotency key, optimistic version check, event, and creator delivery.
No note is required. Explicitly handle, then acknowledge the inbox separately:
reading, polling, replying, and `inbox-ack` do not handle an item.

Linked claims retain their independent lifecycle. Atomic `claim-review` verdict
notifications use receipt policy because the claim owns the review workflow.
Item, queue, inbox, and activity metadata expose effective `completion_policy`.
The per-creator/recipient/session route bound remains 100 open items, shared by
both policies. A creator's `queue_limit` error includes only
`details: {"limit":100,"open_items":100}`; handling frees a route slot.

Schema 7 rebuilds the items table transactionally, preserving IDs, versions,
claim links, indexes, triggers, history, and deliveries. Foreign-key integrity is
checked before commit; a failed migration rolls back the schema and data.

Every update includes the version last observed by the caller:

```console
krab --token-file ~/.config/imitation-krab/worker-one.token \
  status itm_0123456789abcdef0123456789abcdef in-progress \
  --project imitation-krab \
  --session ses_0123456789abcdef0123456789abcdef \
  --expected-version 1
```

Stale versions fail with `409 version_conflict` rather than overwriting newer
state. The JSON error includes `expected_version` and `current_version` when the
current value is known. Authorization failures for reviews include the roles
that can act.

## HTTP API

All endpoints except `/v1/health` require `Authorization: Bearer <token>`.
Every `POST` and `PATCH` also requires an `Idempotency-Key` containing 8–128
restricted ASCII characters.

The CLI creates a fresh key automatically. After a timeout or otherwise
uncertain response, its error prints the exact global `--idempotency-key` to
use for a safe retry. For mutation commands only, the option is accepted either
before the command or after its subcommand arguments. Reusing a key with
different content is rejected.

```text
GET   /v1/whoami
GET   /v1/projects
GET   /v1/projects/{project}/activity?after=0&limit=50&wait=30
GET   /v1/projects/{project}/issues?status=under_review&assignee=worker-one
POST  /v1/projects/{project}/issues
POST  /v1/projects/{project}/issues/import
POST  /v1/projects/{project}/issues/{claim}/assign
POST  /v1/projects/{project}/issues/{claim}/claim
POST  /v1/projects/{project}/issues/{claim}/release
POST  /v1/projects/{project}/issues/{claim}/review
GET   /v1/projects/{project}/issues/{claim}
PATCH /v1/projects/{project}/issues/{claim}/status

GET   /v1/projects/{project}/pull-requests?status=active&assignee=worker-one
POST  /v1/projects/{project}/pull-requests
POST  /v1/projects/{project}/pull-requests/import
POST  /v1/projects/{project}/pull-requests/{claim}/assign
POST  /v1/projects/{project}/pull-requests/{claim}/claim
POST  /v1/projects/{project}/pull-requests/{claim}/release
POST  /v1/projects/{project}/pull-requests/{claim}/review
GET   /v1/projects/{project}/pull-requests/{claim}
PATCH /v1/projects/{project}/pull-requests/{claim}/status

GET   /v1/projects/{project}/inbox?unread=1&limit=50&wait=30
POST  /v1/projects/{project}/inbox/ack

GET   /v1/projects/{project}/sessions
POST  /v1/projects/{project}/sessions
POST  /v1/projects/{project}/sessions/{session}/close

POST  /v1/projects/{project}/sessions/{session}/items
GET   /v1/projects/{project}/sessions/{session}/items/{item}
PATCH /v1/projects/{project}/sessions/{session}/items/{item}/status
POST  /v1/projects/{project}/sessions/{session}/items/{item}/notify

GET   /v1/projects/{project}/sessions/{session}/queue?after=0&limit=50&wait=30
GET   /v1/projects/{project}/sessions/{session}/queue?latest=1&limit=50
```

Session queue and project activity cursors are caller supplied. The project
inbox additionally has an explicit durable cursor per authenticated user and
project. Long-poll waits are capped at 30 seconds and two concurrent waits per
user across these endpoints. `latest=1` is private, bounded, and incompatible
with long polling or a nonzero `after` cursor. A reminder has no text body and
is throttled. Every JSON response, including errors and health checks, carries
the non-secret `X-Krab-Version` response header.

Responses keep provenance separate from content:

```json
{
  "trusted_metadata": {
    "item_id": "itm_...",
    "project_key": "imitation-krab",
    "session_id": "ses_...",
    "status": "open.pending",
    "version": 1
  },
  "untrusted_text": {
    "title": "Review authentication changes",
    "body": "..."
  },
  "content_risk_flags": []
}
```

Client software must preserve that trust distinction. Encoding text as JSON
does not make its instructions trustworthy.

Versioned response schemas and their command mapping are in
[`schemas/`](schemas/README.md). They describe envelopes, not authority.

An `external_id` appears in authenticated metadata because the server has
validated its restricted representation and immutable registry relationship.
It is still only a coordinator/admin-supplied local reference, not proof about
GitHub or permission to perform any external action.

## Limits

- Request body: 32 KiB
- Incomplete request headers: 1-second connection deadline
- Work-item body: 16 KiB
- Status note: 4 KiB
- Title: 512 UTF-8 bytes
- Queue page: 100 events
- Authenticated reads: 120 requests per user per rolling minute
- Mutations: 30 requests per user per rolling minute
- Concurrent long polls per user: 2
- Open items from one sender to one recipient in one session: 100
- Active sessions created by one user in one project: 50
- Events per work item: 128, with a terminal transition still permitted
- External work identifier: 256 restricted ASCII characters
- Identifiers per additive import: 100
- Work claims per project: 10,000
- SQLite database: 256 MiB maximum
- Reminder cooldown: 60 seconds per sender and item
- Session participants: 32

Control and formatting characters are rejected rather than silently removed.
Visible Unicode is retained. Usernames and machine identifiers use restricted
ASCII.

## Test

```console
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The suite exercises the service and complete HTTP handler through local socket
pairs, so it does not require permission to bind a network port.

Focused receipt-policy authority, migration, and lifecycle checks (also suitable
for an operator's focused mutation run):

```console
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3 -m unittest test_receipt test_v050.V050ServiceTests.test_review_verdict_is_atomic_idempotent_and_has_round_history -v
```

Coverage is measured with branch tracking and must remain at or above 85%:

```console
PYTHONPATH=src python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report
```
