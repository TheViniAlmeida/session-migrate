# Claude Desktop 2.1.309: bounded transcript projection

**Version:** `0.12.0.dev17` · **Date:** 5 November 2026. This is a
**source-reader contract**, not certification of Claude Desktop's native replay,
writer, session nesting metadata, token accounting or complete internal context.

## What changed

The tree inventory recognizes a transcript whose exact source version is
`2.1.309`. Both individual migration and tree migration can project repeated
message UUIDs when their portable content and ancestry agree under the narrow
contract below. Source versions are never rewritten to another release.

The source remains **UNKNOWN** and retains `unvalidated_source_version`.
`2.1.309` is deliberately absent from the validated native-writer registry.
Other validated versions keep their existing contracts. Unknown future versions
are not admitted to tree inventory by this change.

## Duplicate envelope contract

Only exact-309 `user`, `assistant`, `attachment`, and
`system`/`stop_hook_summary` records can qualify. Attachments and stop-hook
summaries remain opaque, including their original occurrence. Other system
subtypes and repeated compact boundaries are not accepted. A duplicate
must contain an explicit UUID, parent, session identity, timestamp and version.
The complete JSON must be equal, including JSON types, list order and presence
versus absence of fields, except for these top-level envelope fields:

| Field | Treatment |
| --- | --- |
| `cwd` | A conflicting duplicate working directory is not selected implicitly; supply an explicit destination directory. |
| `slug`, `promptId` | Not promoted into replayed conversation content. |
| `toolUseResult` | May contain additional information; omission is explicitly accounted for, not assumed harmless. |

`message`, `attachment`, tool input/output blocks, `parentUuid`,
`logicalParentUuid`, `sessionId`, timestamps, version, role, sidechain and
compaction flags, and every unlisted field must remain equal. A conflict is an
error, even when choosing one copy would make the graph traversable. This is not
a general UUID-only deduplicator.

The first occurrence represents the common content. Explicit `last-prompt`
selection still wins; otherwise the last **unique** conversation record selects
the leaf. A late appended copy cannot rewind the fallback leaf. The selected
ancestry still determines message/tool order, including child-before-parent
physical records.

Every omitted physical copy has an OPAQUE accounting event with physical
provenance. The original source byte digest and raw record count remain intact;
the migrator does not rewrite the input file. Extra envelope content is not
copied into the destination as instructions or fabricated user messages.

## Boundaries that remain closed

- Missing active parents, missing selected leaves, conflicting duplicates and
  invalid ancestry cycles remain errors. No links are invented or silently cut.
- Compaction is projected only through the existing boundary/summary handling.
  Removed context is not reconstructed. Compaction and inactive branches retain
  their loss/unknown accounting; accepting a duplicate never proves full history.
- `parentUuid` links messages within one transcript. It does not identify the
  persistent parent session. Desktop tree relationships still require an
  explicit operator-reviewed inventory.
- This does not read Claude Desktop's private databases or add a native Desktop
  hierarchy adapter. Tool result blocks inside `message.content` and top-level
  `toolUseResult` wrappers are deliberately treated differently.

## Using the tree workflow

Use the existing [tree guide](tree-migration.md), supplying the actual source
version instead of relabeling the files:

```bash
smigrate tree inventory --profiles /absolute/path/profiles.json \
  --source-profile claude-source --source-client-version 2.1.309 \
  --manifest /absolute/path/inventory.json
```

Use the same version for `tree review` and `tree migrate`. Specify the
OpenCode destination profile, destination `--cwd`, model and provider, and review
`UNKNOWN` coverage and losses before `--acknowledge-partial`/confirmation.
The destination contract remains OpenCode **2.2.5**, including connected-server
import/export. No new command or HTTP endpoint is introduced; TUI and web use the
existing shared parser and inventory workflow.

## Evidence and limits

The initial three synthetic reproductions were: readable ordinary 309 JSONL,
rejected metadata-only duplicate UUIDs, and a genuinely missing-parent failure.
Regression fixtures contain invented content/identities only. They exercise
message/tool ordering, equality conflicts, compaction, loss accounting and the
whole-tree preflight before any destination import.

Public reports motivate the conservative boundary: [upstream #102559](https://github.com/anthropics/claude-code/issues/102559)
describes re-appended envelopes and also changed parent links; [upstream #107157](https://github.com/anthropics/claude-code/issues/107157)
reports replaying previously compacted context in Desktop 2.1.309. These are
reporter observations, **not** a manufacturer format specification or proof that
the first or last native copy always has the right replay semantics. The support
here is defined and tested as portable projection with explicit uncertainty.

### Unresolved compaction topology

A separate synthetic 768-record fixture still fails closed: a compact boundary
has `parentUuid: null` and a missing `logicalParentUuid` listed only in
`preservedMessages.allUuids`. Its persisted `uuids` exist, but their list order
is not the ancestry order. The summary anchor reaches that boundary through
three attachment records; a later boundary also has a continuation pointing at
the preserved old tail rather than its summary. Simply substituting the tail,
ignoring the missing pointer, or selecting the last boundary could change the
conversation or omit a summary.

This release does **not** repair that topology. The full synthetic fixture and a
small regression reproduce the refusal. A correct native compaction-relink
contract, including running-turn sends and physical-versus-memory IDs, needs a
separate validation gate. `allUuids` membership alone is not proof that an absent
record can be discarded or that an arbitrary edge can be created.

### Recovery is not an archival importer

There is currently no supported command that bypasses a rejected Claude graph
and imports every physical record as an archival conversation. `convert`,
`import`, `transfer` and tree nodes all parse the source. `handoff` selects events
from an already parsed, observed source; it cannot rescue a parser rejection.
`reconcile` and `tree resume` recover publication/ledger state, not missing source
ancestry. Codex's `archived_sessions` is a storage lifecycle, not such a mode.

A safe future alternative would be a **separate explicit archival policy**:
retain the immutable original and its hash; render all retained messages with
physical ordinals and original timestamps; declare the chosen order rather than
assert active-graph equivalence; list gaps, duplicate conflicts and omitted
nonportable fields; preserve tool evidence as historical data; and publish only
new IDs after review. Such a mode would not qualify for FULL, native update/CAS
or automatic continuation proofs. It is a design option, not a shipped command.

DEV17 supports the **309 inventory and tree publication path**, as well as
individual conversion, when the parser accepts each selected transcript. An
explicit family inventory drives the existing `target_parent_id` API field
(and OpenCode `parentID`), publishing parents first. Supplying that field does
not bypass source parsing or resolve the five reported compaction cases.

## Release validation

The local selected suites passed **668 tests**: 328 parser/conversion/tree
regressions, 325 audit/projection/operation/usage regressions, 13 CLI checks and
2 real OpenCode 2.2.5 gates (standalone and connected server). The native gates
used private filesystem/network namespaces, four-node families, cold server
reopen and independent child continuation. The private synthetic provider saw no
duplicate prefix or wrapper-only content. The shared daily server was not used.

Initial development failures were retained: missing loss classification and
missing source projection were corrected; a confirmation test was aligned with
the existing commit-time guard. A native run started before the projection fix
timed out on its first case; the final fresh run passed both cases. Independent
review passed the core and compaction-refusal regressions. Ruff and diff checks
passed. The wheel's 91 package files were compared to the reviewed sources.

This does not certify Claude Desktop native replay or the five unresolved
compaction transcripts. No real conversation was read or migrated, and this
release preparation does not update existing installations automatically.

### Reviewed archival of unsettled tool calls

A transcript that parses is not automatically eligible for publication. The
strict `portable-projection-v1` policy still rejects `open_tool_call`. Reviewed
tree operations targeting **OpenCode 2.2.5** can now select the versioned
`portable-opencode-225-archive-v1` policy when the source has an open call.
The existing `reviewed_tree_projection` core request opt-in is bound to the
operation's options hash; the tree coordinator requires explicit acknowledgement
of partial projection before any import.

| Source tool result | Parser calls/results | Reviewed tree behavior |
| --- | --- | --- |
| Absent | 1 / 0 | Archive the call as `ImportedIncompleteTool`, with a declared loss |
| Empty string | 1 / 1 | Preserve a completed result |
| Empty list | 1 / 1 | Preserve a completed result |
| Only `tool_reference` | 1 / 1 | Preserve the result; declare reference-content loss |

Empty and reference-only results are never silently removed by the parser or
audit normalization. The archive transformation preserves the call ID, name and
input and creates an explicit **target-side error**, not a successful source
result. The source witness still reports the open call. A generated relation
binds each target error to its original call, and the expected projection is
computed independently of the writer.

Prepare and observed-export verification check the raw typed marker, ID, name,
input and exact error message. Replacing it with a same-text `ToolError`, success,
a marker on another call, or extra/missing markers does not pass. Only genuinely
open calls qualify: missing names/IDs, duplicate calls/results, orphan or
ambiguous results and identity mismatches remain errors. Other target versions
and the default strict projection policy are unchanged.

Every archived call contributes explicit losses. Source coverage **UNKNOWN**
stays UNKNOWN; known source coverage can be PARTIAL. No archive projection with
generated errors receives a completeness certificate or delta/update authority.
The direct CLI does not implicitly enable the reviewed-tree opt-in. Use the
reviewed tree workflow, including its existing `--acknowledge-partial` step.

Cold reconciliation support is not expanded: the existing native-format and
UNKNOWN proof gates can still return `context_unavailable` or
`proof_unavailable`. Uncertain imports are never automatically repeated.
Completed tree nodes remain protected by their existing source/target/ledger
checks; organizational parents are verified independently of tool archival.
The five unresolved compaction inputs are still refused before this stage.
