# Krab 0.7.0 MGB integration roadmap

- **Status:** one accepted bounded feature; implementation not started
- **Baseline:** current `main` after receipt-policy delivery / schema 7
- **Release purpose:** provide the smallest server-attested transport relation
  needed for Magic Glass Box interactions without importing orchestration
  policy into Krab

## Accepted scope: immutable reply binding

Krab 0.7.0 will allow a newly created session item to carry one optional,
immutable relation to the exact earlier item it answers. The request field and
trusted response field are named `in_reply_to_item_id`.

Example request:

```json
{
  "to": "request-creator",
  "title": "Response",
  "body": "Untrusted response prose.",
  "in_reply_to_item_id": "itm_0123456789abcdef0123456789abcdef"
}
```

Krab attests only the relationship. It does not attest that the prose is
correct, sufficient, approving, or even responsive.

### Validation semantics

When `in_reply_to_item_id` is supplied, Krab must establish in the item-creation
transaction that:

- the referenced item already exists;
- it belongs to the same project and session;
- the new creator was the referenced item's recipient;
- the new recipient was the referenced item's creator; and
- the stored relationship cannot later change.

A malformed item ID is a validation error. Missing, inaccessible,
cross-project, cross-session, and wrong-route targets receive the same
scope-hiding not-found response. The referenced item's current status does not
matter. More than one item may answer the same item, and alternating reply
chains are allowed.

Because the target must pre-exist and the relation is immutable, API-created
cycles are impossible. Krab will not add graph traversal or cycle-detection
machinery.

The reply target is part of the canonical idempotency request. Replaying the
same key with another target is an idempotency conflict.

### Response and visibility contract

`in_reply_to_item_id` is returned as a required nullable trusted-metadata field
on:

- item-creation responses;
- participant-authorized item detail;
- private session-queue events; and
- private project-inbox events.

Legacy and unrelated items return `null`.

The relation is not included in the project-visible activity feed. Exact
conversation topology remains participant-scoped metadata even though project
activity continues to expose its existing minimal item metadata and title.

The CLI adds only:

```console
krab send ... --in-reply-to ITEM_ID
```

There is no separate reply endpoint, reply browser, or thread-management
command in 0.7.0.

## Consumer responsibility

Magic Glass Box remains responsible for deciding what constitutes an acceptable
interaction response. It must continue verifying the expected project,
session, creator, recipient, and item state, and additionally require:

```text
response.in_reply_to_item_id == interaction.request_item_id
```

Titles and bodies remain untrusted text. Krab does not know about MGB runs,
interactions, approvals, or control-room state.

## Compatibility and rollout

This is an additive JSON field but a breaking published-schema change because
Krab's response schemas are closed with `additionalProperties: false`. Strict
consumers must update before receiving 0.7.0 item responses.

The coordinated rollout order is:

1. MGB accepts and preserves the nullable field without yet requiring it.
2. Krab 0.7.0 deploys schema 8, the request field, serializers, schemas, and
   CLI flag.
3. MGB requires an exact reply binding before accepting an interaction
   response.

Schema 8 migrates existing items without rewriting their prose, status,
history, claim link, or completion policy; their reply relation is `null`. A
verified schema-7 backup is required before deployment.

New clients attempting to send the field to an older Krab server will be
rejected as an unknown request field. Release notes and deployment notices must
state both directions of this compatibility boundary.

## Explicit non-goals

The reply relation does not:

- change or complete either item's status;
- acknowledge a receipt-policy item;
- mutate or infer claim state;
- make text trusted or validate its meaning;
- create general threading, subjects, reply lists, participants, or
  conversation search;
- expose private bodies or relationship metadata through project activity; or
- add MGB-specific endpoints, fields, identities, or workflow behavior.

## Acceptance checks

0.7.0 is not ready until tests prove:

1. A direct recipient can create a reverse-route reply and both participants
   observe the exact trusted link.
2. Malformed, missing, inaccessible, cross-project, cross-session, and
   wrong-route targets fail without widening visibility.
3. The link survives item detail, private queue, project inbox, and idempotent
   replay unchanged.
4. Changing the target under a reused idempotency key conflicts.
5. Legacy rows migrate to `null` and the migrated database passes foreign-key
   and integrity checks.
6. Multiple replies and terminal-item targets work without status coupling.
7. Project activity does not disclose the relation.
8. Published schemas and CLI JSON match the wire contract.
9. Focused mutations of scope, route reversal, immutability, idempotency, and
   serialization are killed.
10. Existing claim links, completion policies, private authorization, and
    text-provenance boundaries remain unchanged.

Other audit follow-ups remain candidates rather than implied 0.7.0 scope.
