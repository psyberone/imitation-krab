# imitation-krab

`imitation-krab` is a local, scoped work mailbox for cooperating software
agents. It is intentionally not a general chat service, workflow engine, or
agent runtime.

Each user has one server-generated bearer token and a private delivery queue.
Every work item belongs to exactly one project and one session. Users can place
work into another session participant's queue, update work through a fixed
status machine, and send a bodyless reminder. They cannot inspect another
user’s queue or open a peer-to-peer connection.

Each project also has two shared claim queues: issues and pull requests. They
are filtered views over one small local registry, not GitHub integration.
Project coordinators and admins can register or additively import opaque
external identifiers and assign available work. Members can atomically claim
available work for themselves and submit completed work for independent
review. A different coordinator or admin returns it for changes or approves
and completes it. A work item may carry one immutable link to a registry entry.

The v0 daemon binds to `127.0.0.1` by default, uses SQLite, and has no runtime
package dependencies beyond Python 3.11 or newer. Its explicit container mode
binds the container namespace while the supplied Compose deployment publishes
the API only on host loopback.

## Security boundary

Message text is always untrusted. The API separates server-authenticated
metadata from user-controlled text, rejects dangerous terminal and Unicode
format controls, and flags obvious prompt-injection language. Those flags are
advisory: natural-language prompt injection cannot be solved by string
filtering.

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
operator terminal, never from an agent prompt:

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

New images migrate an existing schema 1, 2, or 3 database to schema 4 at startup without
deleting users, projects, sessions, messages, or claims. Take a verified backup
before updating, then rebuild and recreate the service container:

```console
docker compose --profile admin run --rm krab-admin \
  admin backup "/var/lib/imitation-krab/backups/krab-before-v4.db"
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
data volume.

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

Token files are created with mode `0600`. The CLI refuses symlinked or
hard-linked token files, non-owned files, group/world-accessible files, and
token directories not owned by the current user with mode `0700`. If
`--write-token` is omitted, the token is printed exactly once.

Project roles are deliberately fixed:

| Role | Project capabilities |
| --- | --- |
| `member` | Read claim queues, self-claim, activate or submit assigned work, release claimed/active work, and participate in sessions |
| `coordinator` | Member capabilities plus claim creation/import, explicit assignment, review of another assignee's work, and stale/review-claim release |
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

Global options such as `--token-file` precede the command:

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

Console output escapes terminal controls and labels every user-controlled field
as content. Add the global `--json` option for machine-readable output.

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
admin reviews it. They can request changes:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-status clm_0123456789abcdef0123456789abcdef active \
  --project imitation-krab --kind issue --expected-version 4
```

After the assignee resubmits, the reviewer can approve and complete it:

```console
krab --token-file ~/.config/imitation-krab/primary-dev.token \
  claim-status clm_0123456789abcdef0123456789abcdef done \
  --project imitation-krab --kind issue --expected-version 6
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

Review explanations still belong in session work-item messages. Claims do not
gain prose, labels, priorities, dependencies, reservations, or automatic
notifications, and Krab neither queries nor mutates GitHub.

Link a message at creation time with `send --claim clm_...`. The link, project,
kind, and external identifier are immutable. Claim state and message status are
independent: neither lifecycle silently changes the other.

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

Every update includes the version last observed by the caller:

```console
krab --token-file ~/.config/imitation-krab/worker-one.token \
  status itm_0123456789abcdef0123456789abcdef in-progress \
  --project imitation-krab \
  --session ses_0123456789abcdef0123456789abcdef \
  --expected-version 1
```

Stale versions fail with `409 version_conflict` rather than overwriting newer
state.

## HTTP API

All endpoints except `/v1/health` require `Authorization: Bearer <token>`.
Every `POST` and `PATCH` also requires an `Idempotency-Key` containing 8–128
restricted ASCII characters.

The CLI creates a fresh key automatically. After a timeout or otherwise
uncertain response, its error prints the exact global `--idempotency-key` to
use for a safe retry. Reusing a key with different content is rejected.

```text
GET   /v1/projects
GET   /v1/projects/{project}/issues?status=under_review&assignee=worker-one
POST  /v1/projects/{project}/issues
POST  /v1/projects/{project}/issues/import
POST  /v1/projects/{project}/issues/{claim}/assign
POST  /v1/projects/{project}/issues/{claim}/claim
POST  /v1/projects/{project}/issues/{claim}/release
PATCH /v1/projects/{project}/issues/{claim}/status

GET   /v1/projects/{project}/pull-requests?status=active&assignee=worker-one
POST  /v1/projects/{project}/pull-requests
POST  /v1/projects/{project}/pull-requests/import
POST  /v1/projects/{project}/pull-requests/{claim}/assign
POST  /v1/projects/{project}/pull-requests/{claim}/claim
POST  /v1/projects/{project}/pull-requests/{claim}/release
PATCH /v1/projects/{project}/pull-requests/{claim}/status

GET   /v1/projects/{project}/sessions
POST  /v1/projects/{project}/sessions
POST  /v1/projects/{project}/sessions/{session}/close

POST  /v1/projects/{project}/sessions/{session}/items
GET   /v1/projects/{project}/sessions/{session}/items/{item}
PATCH /v1/projects/{project}/sessions/{session}/items/{item}/status
POST  /v1/projects/{project}/sessions/{session}/items/{item}/notify

GET   /v1/projects/{project}/sessions/{session}/queue?after=0&limit=50&wait=30
```

Queue cursors are maintained independently for each project/session. Long poll
waits are capped at 30 seconds. A reminder has no text body and is throttled.

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

An `external_id` appears in authenticated metadata because the server has
validated its restricted representation and immutable registry relationship.
It is still only a coordinator/admin-supplied local reference, not proof about
GitHub or permission to perform any external action.

## Limits

- Request body: 32 KiB
- Work-item body: 16 KiB
- Status note: 4 KiB
- Title: 512 UTF-8 bytes
- Queue page: 100 events
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
