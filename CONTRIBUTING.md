# Contributing to imitation-krab

Thank you for helping improve imitation-krab. The project is deliberately a
small, local coordination service rather than a general chat platform,
workflow engine, agent runtime, or integration hub. Focused fixes and narrow
features that preserve that boundary are welcome.

Participation in the project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before opening an issue

- Search the existing issues first.
- Use the bug form for reproducible incorrect behavior.
- Use the feature form to explain the concrete coordination problem and the
  smallest server-side change that would solve it.
- Keep credentials, bearer tokens, private message content, database files,
  and other sensitive data out of issues and logs.
- Do not publicly disclose a suspected vulnerability. Follow
  [SECURITY.md](SECURITY.md) instead.

Feature proposals should explain why the behavior belongs in Krab rather than
in an agent, an external orchestrator, GitHub/GitLab, or an operating-system
sandbox. Repository discovery, code execution, credential brokering, plugins,
remote platform synchronization, and general workflow configuration remain
outside the project.

## Development setup

The runtime uses Python 3.11 or newer and has no third-party package
dependencies. Run the source tree without installing it:

```console
PYTHONPATH=src python3 -m imitation_krab --version
```

For an editable environment:

```console
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps -e .
```

Do not put development databases, pepper files, tokens, or exported backups in
the repository.

## Tests and verification

Run the complete suite from the repository root:

```console
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests \
  python3 -m unittest discover -s tests -v
```

Coverage is branch-aware and must remain at or above 85%:

```console
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m coverage run --branch -m unittest discover -s tests
python3 -m coverage report
```

For behavior changes:

1. Start with a failing test that demonstrates the contract.
2. Cover both the successful path and relevant rejection/rollback paths.
3. Temporarily introduce a focused mutation in the new behavior and confirm
   the relevant test fails, then restore the implementation. A mutation-testing
   package and survivor hunt are not required.
4. Run the complete suite after focused verification.

Keep tests deterministic, local, and independent of network services. The HTTP
tests use local socket pairs and should not require permission to bind a port.

## Contracts, storage, and security

- Keep authenticated control metadata separate from `untrusted_text`.
- Never derive identity, authority, routing, or execution from message prose.
- Preserve project, session, participant, and delivery isolation.
- Treat idempotency, optimistic concurrency, and atomic rollback as public
  behavior rather than implementation details.
- Add an explicit, transactional migration for every database schema change.
  Test migration from the immediately previous schema and verify foreign-key
  integrity and rollback behavior.
- Update the schemas under `schemas/v1/` when a response shape changes. Those
  schemas are closed, so even an additive response field can break strict
  consumers.
- Update README and security documentation when commands, deployment steps,
  visibility, trust boundaries, or compatibility change.

Do not weaken loopback-only deployment, token handling, terminal-safe output,
bounded pagination/long polling, or database-size protections without a clear
threat-model review.

## Pull requests

Keep each pull request focused. Include:

- the problem and the deliberately bounded solution;
- explicit non-goals when scope could be misunderstood;
- related issue links, if any;
- the exact verification commands and results;
- migration and compatibility notes when applicable; and
- documentation/schema updates required by the change.

Review your own diff before requesting review. Remove debug artifacts and
generated state, and do not mix unrelated formatting or refactoring into a
behavioral change.

Agent-assisted contributions are welcome. The submitting contributor remains
responsible for reviewing the resulting diff, preserving source attribution,
running the verification, and explaining the change accurately.
