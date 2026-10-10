# Durable migration records

Real `import` and `transfer` operations record their intent before invoking the
native installer. `convert`, dry runs and `proposal` do not create a lineage
store. A proposal is review metadata, never a saved command that can be replayed.

The state directory comes from the migrator's base environment, before source
and destination profiles select their own HOME/XDG directories. It is
`$XDG_STATE_HOME/session-migrate/lineage-v1`, or
`$HOME/.local/state/session-migrate/lineage-v1` when XDG_STATE_HOME is absent.
Programmatic callers can supply `migration_state_home`; an explicitly empty
environment must provide a usable state location for a real import.

The ledger contains references, identities, policy versions, hashes, counts and
operation states. It excludes conversation bodies and tool payloads. Private
directories and files must meet the storage ownership and permission checks;
the tool does not relax existing permissions or repair corrupt stores.

## What successful verification means

Claude, Codex and supported OpenCode exports have a raw auditor and an
independent expected projection. The auditor accounts for native records and
also checks whether the parser retained their semantic fields. These are
separate checks: counting every input record does not prove lossless parsing.
The expected projection is derived from the captured source, rather than from
the serialized destination as its own oracle. Verification reads the installed
destination again and compares its actual projection with that expectation.

Source and destination checkpoints remain separate. Native identity requires
an observed loader/capture and matching root/layout/ID; a portable transcript
does not automatically become a tracked native source. Unsupported or partial
evidence stays explicit. The programmatic projection API reports metadata loss,
including an omitted default title, separately from conversation-content loss;
this does not add fields to the existing CLI success JSON.

These records establish no closed-turn proof, delta eligibility or permission
to update an existing native session. Native clients can still modify their
sessions independently; ledger locks coordinate migrators using the same state
directory and are not native-client locks.

## Interrupted imports

Cancellation closes immediately before publication intent is persisted and
synchronized. It cannot interrupt that persistence or the subsequent importer.
The tool observes an acknowledged destination before completing its record.
An intent-persistence failure, or a changed source, uncertain native invocation
or failed verification after this gate, returns `needs_reconcile` instead of
automatically retrying. Earlier source changes can require ordinary re-preparation.
CLI error metadata includes the operation ID, reserved target, stage, reference
availability and whether the reconciliation receipt was recorded.

`native_not_started` means the importer was not called. `attempted_unknown`
means it was called without a conclusive acknowledgement. `acknowledged` means
the importer acknowledged publication, while final verification or recording
still needs attention. A reserved ID or planned manifest alone is not proof
that a destination exists.

Inspect the reserved destination and available evidence before taking further
action. Repeating publication could create another session. Reopening a ledger
never restores a live permission to publish, and no saved proposal is imported
automatically. Recovery commands, continuity assessment and native session
updates are separate milestones.

The programmatic result exposes `lineage_receipt` separately. Existing success
JSON and native manifests retain their previous fields and schema. The TUI
retains pending-operation state through the publication gate and presents
reconciliation evidence without running resume commands.

See [storage](lineage-storage.md) and [checkpoint primitives](lineage-checkpoints.md)
for the persistence and hashing contracts.
