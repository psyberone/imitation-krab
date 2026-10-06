# Security model

## Intended deployment

`imitation-krab` is a single-host coordination service. The native daemon
binds only to IPv4 loopback and stores its database and authentication pepper
outside agent workspaces. The supplied container binds its own namespace but
publishes only to host IPv4 loopback. It is not designed to be exposed to a
LAN or the public internet.

The strongest recommended deployment runs the daemon under a dedicated OS
identity and gives each agent access only to its own credential and workspace.
Agent models should invoke a narrow client operation; they should never receive
the raw bearer token in model context.

## Security invariants

1. A message can cause only a database mutation and an inbox event.
2. User text cannot execute code, fetch a URL, grant permission, create an
   identity, change membership, or change scope.
3. Authentication determines the actor. A request cannot supply a trusted
   sender identity.
4. Every work item has one immutable project, session, creator, recipient,
   title, and body.
5. Project and session authorization is checked on every object access.
6. Only the creator and recipient can read an item.
7. Status changes require an allowed actor transition and the current version.
8. Notifications are server-generated events. A reminder has no message body.
9. Administrative identity and membership operations are unavailable over the
   HTTP API.
10. No token, authorization header, request body, or work-item content enters
    the structured request log.
11. Shared issue and pull-request claims are visible only to project members;
    only a project coordinator or admin can create or import their identifiers.
12. Claim acquisition and assignment are atomic and versioned. Members can
    claim only for themselves. A coordinator or admin can assign only available
    work to an active member of the same project and cannot silently reassign it.
13. Only the assignee can activate or submit their claim. Only a different
    coordinator or admin can return submitted work or approve it as done; role
    membership never permits self-approval. A reviewer verdict, linked private
    notification, delivery event, review-round record, and claim transition
    commit atomically. The assignee can release claimed or active work, while a
    coordinator/admin can also release submitted work. Imported identifiers are
    additive: omission never mutates or removes an existing claim.
14. A message-to-claim link is immutable and cannot cross a project boundary.
    Message and claim lifecycles do not grant authority to one another.
15. A project inbox is a union only of deliveries addressed to the authenticated
    user. Durable acknowledgement is explicit, monotonic, and cannot advance
    beyond that user's delivered events in the project.

## Trust boundaries

### Trusted control data

The server owns user IDs, authenticated sender identity, project/session
relationships, timestamps, event sequence numbers, status, versions, and
routing. These values are never derived from message prose.

Claim kind, assignment, effective state, review marker, review round, version,
and linkage are server-enforced control data. A verdict action is structured
control data; its body remains untrusted prose. An external work identifier is
restricted ASCII and immutable once registered, but remains a
coordinator/admin-supplied local reference. Its presence does not prove that a
GitHub object exists or authorize any GitHub operation.

Project roles are fixed rather than user-defined. Members perform work,
coordinators may populate and manage the project claim queue, and admins retain
the coordinator capabilities plus administrative override. Identity,
membership, and role changes remain unavailable over HTTP.

### Untrusted content

Project and session labels, titles, bodies, notes, and result summaries remain
untrusted even when an authenticated user supplied them. API responses place
them under `untrusted_text`; consumers must not merge them into a system or
developer instruction channel.

External work identifiers are not prose fields, but they are still supplied by
project coordinators/admins and are not externally verified. Consumers must
treat them as opaque labels, never as instructions, URLs to follow, or proof of
authority.

### Execution

Execution is outside the service. `imitation-krab` contains no command runner,
hook, callback, webhook, plugin loader, URL fetcher, or repository integration.
An agent host must keep its tool permissions fixed regardless of received
content.

## Threats and controls

| Threat | Primary controls |
| --- | --- |
| Unauthenticated local process | 256-bit bearer credentials, constant-time digest comparison, generic authentication errors |
| Stolen database | Token digests are keyed with a separate 256-bit pepper; raw tokens are never stored |
| Stolen bearer token | Per-user least privilege, immediate rotation/revocation, project/session membership, rate limits |
| Cross-project or cross-session access | Scoped route checks plus composite SQLite foreign keys |
| Sender impersonation | Sender always comes from authentication; no accepted `sender_id` field |
| Work-item bait-and-switch | Delivered title/body/scope/parties are immutable |
| Lost concurrent update | Required expected version and transactional update |
| Duplicate work ownership | Immediate write transaction, one assignee, fixed claim transitions, and optimistic version checks |
| Queue poisoning by a member | Claim creation and import require coordinator or admin role |
| Malicious or incomplete import | Restricted identifier-only schema, 100-entry cap, duplicate rejection, atomic additive transaction, and no deletion or mutation by omission |
| Unauthorized assignment | Coordinator/admin check, active same-project assignee lookup, available-only transition, expected version, and audit entry |
| Self-approval or forged review | Assignee identity check, independent coordinator/admin requirement, fixed transitions, expected version, and distinct audited verdicts |
| Verdict transition without notifying the assignee | Dedicated idempotent transaction creates the linked item and delivery with the claim transition or rolls all of it back |
| Stale or abandoned claim | Assignee release for claimed/active work; coordinator/admin release including atomic review-marker cleanup; reassignment remains an explicit release followed by assignment |
| Forged/cross-project work link | Opaque claim IDs, membership checks, foreign key, same-project trigger, and immutable-link trigger |
| Malicious external identifier | Restricted ASCII and length, opaque-reference semantics, no URL parsing/fetching, and no external action |
| Retry duplication or replay confusion | Required idempotency keys bound to actor, operation, and request hash |
| Cross-session inbox disclosure | Project membership plus authenticated-user delivery rows and session-membership joins; no project-wide broadcast rows |
| Lost or forged read position | Explicit per-user/project monotonic acknowledgement bounded by the highest event delivered to that same user |
| Queue flooding | Request throttles, per-user long-poll concurrency, participant/session/event limits, message-size limits, per-route open-item cap, and database-size cap |
| Terminal/log injection | Dangerous controls rejected on ingress; console escapes again on output; structured logs omit bodies |
| JSON ambiguity | Strict UTF-8, body cap, duplicate-key rejection, finite JSON numbers, unknown-field rejection |
| Unicode identity spoofing | Handles, project keys, IDs, statuses, and idempotency keys use restricted ASCII |
| Prompt injection | Explicit untrusted-content envelope, risk flags, no automatic execution, and authority determined only by structured policy |
| Direct-message abuse | No general message endpoint; text exists only on immutable work items and status events; reminders are bodyless |

## Content validation

The server enforces valid UTF-8 and rejects:

- NUL, escape, C0, and C1 control characters except newline and tab;
- Unicode formatting controls, including bidirectional overrides and zero-width
  formatting characters;
- Unicode line and paragraph separators other than ASCII newline;
- Unicode noncharacters;
- oversized fields, lines, line counts, requests, and participant lists.

The console renderer independently converts any unexpected control or format
character to visible `\\uXXXX` notation. Output encoding is a separate boundary
even though ingress validation should already have rejected those characters.

Deterministic patterns add advisory flags for instruction overrides, role
delimiters, credential requests, and external URLs. These flags deliberately do
not rewrite content or grant/deny authority. Semantic filters are bypassable and
can also flag legitimate security discussions.

## Credential handling

Tokens have the form `krab_usr_<opaque-id>_<256-bit-secret>`. The public user ID
allows direct lookup; the whole token is verified using HMAC-SHA-256 with a
server pepper. Tokens are displayed once or written to a new mode-`0600` file
inside an owned mode-`0700` directory. The client refuses token symlinks, hard
links, non-owned files, and group/world-readable files.

Token possession is authentication. If multiple agents run under the same OS
account with unrestricted filesystem or process access, they may be able to
steal one another's credentials. Bearer tokens do not solve that boundary.
Use separate OS identities, containers, sandboxes, or a supervisor that owns
credentials and exposes only narrow client operations.

The database is not encrypted at rest. Use the host's full-disk encryption and
filesystem access controls if message confidentiality matters.

### Container deployment

The Compose deployment improves separation only when agents cannot control the
Docker daemon. Docker access would let an agent start another container with
the data, pepper, backup, or host filesystem mounted. Never mount the Docker
socket into an agent and do not grant agent processes permission to invoke the
Docker API.

The server container runs as UID/GID `10001`, uses a read-only root filesystem,
drops all capabilities, enables `no-new-privileges`, and publishes port `8765`
only on `127.0.0.1`. The database and pepper use different named volumes so a
copy of the database volume alone does not contain the token-verification key.
Named volumes do not encrypt their contents; host full-disk encryption remains
required.

`--container-bind` deliberately permits `0.0.0.0` inside the container so
Docker's bridge can reach the service. It is not safe as a general host bind.
The supplied Compose port mapping is part of the security boundary and must
not be changed to an unqualified `8765:8765` mapping.

One user token covers all projects to which that user belongs. Compromise of
that token therefore crosses the user's project memberships; project/session
checks limit other users' data, not that user's own blast radius.

## Known limits

- There is no TLS or remote mode. Do not place a reverse proxy in front of v0.
- Prompt-injection risk flags are telemetry, not prevention.
- A fully compromised daemon account or host can alter state and credentials.
- A compromised authenticated agent can perform every operation granted to its
  identity until the token is revoked.
- The claim registry does not authenticate to, query, or synchronize with
  GitHub. Import accepts only a caller-supplied identifier manifest and is not
  synchronization: duplicate spellings or stale external state are an
  agent/operator concern, and uniqueness covers only the exact project, kind,
  and external ID.
- Claim ownership is cooperative coordination, not a filesystem lock or a
  substitute for GitHub permissions and branch protection.
- Review readiness is a local handoff marker, not proof of a GitHub review,
  branch status, test result, or merge authorization.
- Project inbox delivery and acknowledgement are not presence. The service has
  no heartbeat and cannot distinguish a working, disconnected, paused,
  rate-limited, or dead agent.
- Acknowledgement means only that a client explicitly advanced its Krab cursor;
  it does not prove that an agent understood, accepted, or completed the work.
- There is no automatic retention or archive operation. The 256 MiB database
  ceiling prevents host-disk exhaustion, but a malicious or long-running user
  can still consume that allowance and deny future writes until an operator
  intervenes.
- Backup-volume retention is operator-managed, and exported snapshots contain
  plaintext message data.
- The service cannot enforce filesystem work assignments. Its ownership model
  is cooperative outside the database.
- SQLite files must not be copied live as an ad hoc backup while WAL mode is in
  use. Stop the daemon or use the `admin backup` command, which uses SQLite's
  online backup API and verifies the resulting snapshot.

## Verification expectations

Security-relevant changes should preserve tests for authentication isolation,
scope isolation, fixed status transitions, optimistic concurrency,
idempotency, strict JSON parsing, hostile Unicode/control characters, prompt
risk tagging, immutable content and work links, queue privacy, atomic claim
ownership, coordinator/admin creation and assignment boundaries, additive
import behavior, independent review and self-approval denial, exact scoped
claim filters, release boundaries, and reminder throttling.
