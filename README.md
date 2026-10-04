# imitation-krab

`imitation-krab` is a local, scoped work mailbox for cooperating software
agents. It is intentionally not a general chat service, workflow engine, or
agent runtime.

Each user has one server-generated bearer token and a private delivery queue.
Every work item belongs to exactly one project and one session. Users can place
work into another session participant's queue, update work through a fixed
status machine, and send a bodyless reminder. They cannot inspect another
user's queue or open a peer-to-peer connection.

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

From this directory:

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
  admin user-create alice
docker compose --profile admin run --rm krab-admin \
  admin user-create bob

docker compose --profile admin run --rm krab-admin \
  admin project-create imitation-krab --label "Imitation Krab"
docker compose --profile admin run --rm krab-admin \
  admin project-add-user imitation-krab alice --role admin
docker compose --profile admin run --rm krab-admin \
  admin project-add-user imitation-krab bob --role member
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
Do not run `docker compose down --volumes` unless you intentionally mean to
delete the database, pepper, and backups.

## Bootstrap

Initialize the database, create users, and create a project through the local
administrative CLI:

```console
krab init

krab admin user-create alice \
  --write-token ~/.config/imitation-krab/alice.token
krab admin user-create bob \
  --write-token ~/.config/imitation-krab/bob.token

krab admin project-create imitation-krab --label "Imitation Krab"
krab admin project-add-user imitation-krab alice --role admin
krab admin project-add-user imitation-krab bob --role member
```

Token files are created with mode `0600`. The CLI refuses symlinked or
hard-linked token files, non-owned files, group/world-accessible files, and
token directories not owned by the current user with mode `0700`. If
`--write-token` is omitted, the token is printed exactly once.

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
krab --token-file ~/.config/imitation-krab/alice.token \
  session-create \
  --project imitation-krab \
  --label "Authentication review" \
  --participant alice \
  --participant bob
```

The response contains a server-generated session ID. Use it for every work
item in that unit of work:

```console
krab --token-file ~/.config/imitation-krab/alice.token \
  send \
  --project imitation-krab \
  --session ses_0123456789abcdef0123456789abcdef \
  --to bob \
  --title "Review authentication changes" \
  --body-file request.txt
```

Bob can watch only Bob's private deliveries:

```console
krab --token-file ~/.config/imitation-krab/bob.token \
  queue \
  --project imitation-krab \
  --session ses_0123456789abcdef0123456789abcdef \
  --watch
```

Console output escapes terminal controls and labels every user-controlled field
as content. Add the global `--json` option for machine-readable output.

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
krab --token-file ~/.config/imitation-krab/bob.token \
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
