# Private lineage storage

The first lineage component is a programmatic storage API. It does not yet add
lineage commands, canonicalization, automatic continuity, native update, or a
web dashboard. Existing CLI/TUI transfers retain their current behavior.

`LineageStore.create_new(path)` creates a fresh schema1 store.
`open_existing(path)` validates an existing store; `inspect_existing(path)`
returns absence without creating a missing store. Empty, corrupt or unknown
schemas fail closed. There is no automatic migration of an existing database.

The file is a SQLite main image; SQLite itself runs only in memory. A validated
file descriptor loads it and an exclusive temporary image, fsync and atomic
replacement persist it under locks. No SQLite journal/WAL/SHM is opened on disk.
The private subtree uses owner0700 and regular single-link files use owner0600.
Ancestral paths and file/lock identities are checked around I/O; existing
ancestral permissions are not changed.

Persistent operation and target-namespace locks precede the main store lock.
Readers share the main lock; mutations and conditional state transitions take
it exclusively. Locks coordinate migrators sharing one state namespace. They
provide no exclusion or CAS guarantee against a native harness.

Records contain qualified identities, hashes, counts, versions, references,
states and checkpoints. They do not store conversation bodies, tool payloads,
media bytes, credentials or source titles. Unknown fields and malformed hashes
are rejected. Source identity or projection can be unavailable; receipts retain
that limitation without inventing a native edge or continuity proof.

Source, target input artifact, expected projection and observed export hashes
are separate. Source and target checkpoints are independent. Graph edges require
coherent qualified identities and proof metadata; adapters must supply actual
coverage and projection evidence in the next integration milestone.

A successful `ready` write creates a volatile authorization on its live guard.
That authorization is consumed before publication I/O, and the publication claim
appears only after successful synchronization. Reopening or restoring a `ready`
image does not recreate a permission to publish. Recovery reads metadata and
supports inspection or cancellation; it never executes a saved proposal or
imports a native session automatically.

Views distinguish durable state from conservative effective uncertainty.
Unconfirmed publishing is not a claim that a native import was absent, completed,
or interrupted. Reconciliation receipts report `native_not_started`,
`attempted_unknown`, or `acknowledged` only according to the available evidence.
Planned references are not presented as verified files. Native invocation and
observation remain responsibilities of the upcoming operation integration.

Limits are explicit:64MiB per image,1024 unit/tool-link hashes per collection,
128KiB per checkpoint and a reserved finalization budget for pending operations.
Growth and reservations are enforced under the store lock; uncertain operations
keep their reservation. No history or witness is truncated to fit. Persistence
costs O(image size), including free pages; no vacuum or compaction discards data.
A logical reservation cannot prevent a physical disk failure.

Tests cover fault boundaries, namespace/lock substitution, concurrent processes,
restoration without publication claims, corruption, privacy and finalization
near the capacity limit. Native projection, canonical coverage and continuity
are not proved by these storage tests. See the approved implementation plan for
those separate gates.
