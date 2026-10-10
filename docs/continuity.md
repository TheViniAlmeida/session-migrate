# Continuity and recovery

The migrator records an operation before native publication. A completed import
and a verified continuity checkpoint are separate results: an import with known
projection losses can remain `untracked` for continuity. Existing import/transfer
JSON and manifest v2 retain their previous contracts.

## Inspect existing operations

These commands read metadata without creating a ledger or refreshing a catalog:

```bash
smigrate lineage list --limit 50 --offset 0 --json
smigrate lineage inspect --operation-id OPERATION_ID --json
smigrate --state-home /absolute/migrator-state lineage list --json
```

`--state-home` identifies the migrator state directory containing `lineage-v1`;
it is separate from a native harness home. Without this option, the command
uses the invoking user's base HOME/XDG state environment before profile
selection. It does not relocate the ledger into a destination profile.
An absent ledger produces `ledger_missing` without creating directories.
Listing order is the store's deterministic operation order, not inferred activity.

Metadata includes qualified native identities, checkpoints, durable/effective
states, coverage, policy versions, hashes and available references. It does not
include messages, tool arguments/results, or image bodies. Paths and profile
labels are still private operational metadata.

## Assess a selected operation or pair

```bash
smigrate --catalog /absolute/catalog.sqlite3 assess \
  --operation-id OPERATION_ID --profiles /absolute/profiles.json --json
```

The saved profile file uses the same private profile contract as the terminal
interface. Profiles must still describe the current roots, CLI and environment.
An old fingerprint is not a profile. Without a saved file, catalog roots supply
the existing `root-ID` profile convention; explicit current profiles can be
provided with `--source-profile`, `--source-profile-name`, `--source-format`,
`--source-root`, `--source-cli` and repeated `--source-env KEY=/absolute/path`.
The target has symmetric flags. Supplied values must agree with saved profiles.

To inspect a pair without selecting an operation, provide both complete
references: `--source-format`, `--source-root`, `--source-id`,
`--source-profile`, and their target equivalents. Operation selection and
explicit native session IDs are mutually exclusive. IDs alone cannot identify
sessions in different roots.

Assessments return `no_op`, `delta_candidate`, `conflict`, `lineage_broken`,
`untracked`, or `needs_reconcile`, with evidence and fixed reasons. Current
adapters do not prove native turn closure, so a changed source cannot authorize
an automatic delta. No update, resume, or new import occurs during assessment.
A valid conflict or untracked result exits 0; an operational inspection failure
exits 1; invalid arguments exit 2. Existing CLI commands keep their exit codes.

An existing schema-4 catalog is copied into a bounded, query-only in-memory
SQLite connection. Readonly inspection does not migrate, chmod, create or
refresh it. Catalogs with WAL/journal sidecars, unsupported schemas, unsafe
links or unavailable roots must be refreshed separately using the ordinary
catalog command. Native OpenCode exports can create scratch/cache/WAL runtime
state; the assessment declares those read effects, including failed attempts.

Operations originally made without saved profiles require deliberate
`--legacy-runtime-context` selection. This binds the exact current sparse public
runtime environment. It does not reconstruct a historical environment or
support arbitrary different legacy source/target HOME values.

A stock OpenCode import can add a non-empty session model variant that the
portable parser declares as context loss. If that was absent from a verified
prepared projection, the acknowledged import requires reconciliation rather
than a verified completed checkpoint. The reserved ID remains visible; do not
repeat the import. This was observed with both designated 2.0.22 and 2.0.23 clients.

## Explicit recovery and adoption commands

`reconcile import` and `reconcile transfer` reproduce the complete original
normalized operation options, including its reserved target `--session-id`,
source format and original selectors. Provide `--operation-id`, `--source-file`
and `--input-file`; `--manifest-file` supplies original title-finalization metadata
when needed. For file-backed targets the unchanged published file can also be
the original native input, if its complete digest still matches. A separate
input copy never makes an altered target eligible.

Without `--confirm`, these commands prepare metadata, discard the private handle
and make no ledger/alias/baseline writes. `--confirm` prepares fresh evidence and
commits that live handle in the same process. It does not apply previous JSON.
A blocked confirmation exits 1; a valid preparation result exits 0. Uncertainty
returns the same operation/baseline ID for inspection, without another import.

`--current-catalog` selects the readonly current driver. Global `--catalog`
remains an original operation option; it is not silently replaced in the options
hash. Original implicit/default catalog selectors remain unavailable when the
historical physical catalog binding cannot be proved. Native-ID transfers and
portable imports do not acquire a fictional catalog option.

Current profile flags use `--source-profile` / `--target-profile`, with separate
`--source-profile-format`, `--source-profile-root`, `--source-profile-name`,
`--source-profile-cli`, repeated `--source-profile-env`, and target equivalents.
`--profiles` loads saved current profiles; contradictory flags are rejected.
`--source-native-id` / `--target-native-id` qualify current native references.
An original Profile API transfer requires explicit `--profile-operation`; the
binding is reconstructed through the real selected profiles/catalog, retaining
the original catalog ID. A contradictory original catalog is rejected before
binding. Legacy operations require deliberate `--legacy-runtime-context`;
the exact current sparse public environment must match, with no guessed HOME/XDG.
Portable import recovery preserves an original absent source native identity.

`adopt` requires both qualified current profiles/native IDs and exact
`--source-client-version` / `--target-client-version`. Default prepares an
`adopted_now` baseline proposal; `--confirm` appends the proved current baseline.
Inspect an uncertain commit with `lineage baseline --baseline-id ID --json`.

`lineage cancel-ready --operation-id ID` previews an existing recovered READY
operation. `--confirm` cancels only that operation with durable native-not-started
state. It cannot cancel or mark completed an unknown native import attempt.

## Recovery and adoption core

`prepare_reconciliation` reconstructs evidence from the original request,
source capture and original native input. Only an operation with a durable
native acknowledgement can be completed. A recovered READY operation is
inspectable/cancellable; an unknown import attempt does not become acknowledged
because a target exists. Private one-shot handles cannot be reconstructed from
proposal JSON. Reconciliation does not repeat native publication.

Recovery currently supports file-backed Claude/Codex evidence. Unavailable
original context or native-title proof blocks finalization. Explicit local title
aliases and their receipts must match the same operation, title and binding;
conflicting metadata is not overwritten. An uncertain persisted result is
inspected by the same operation ID.

`prepare_adoption` compares a deliberately selected, freshly captured pair.
Verified content, tool relations and structure are required before an explicit
commit appends an `adopted_now` baseline. This baseline records current evidence,
not an invented historical import or acknowledgement. OpenCode/Kilo adoption
remains unavailable until independent native structure evidence is implemented.
An uncertain baseline commit is inspected by its original baseline ID.

The commands never apply a serialized proposal; they invoke these core functions.
All writes require freshly prepared in-process handles. Migrator locks coordinate
its own callers; they do not provide exclusion or compare-and-swap against an
external coding agent. `update_supported` remains false until the native proof
required by the implementation plan is passed.
