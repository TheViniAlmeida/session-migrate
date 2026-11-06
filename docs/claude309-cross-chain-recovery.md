# Claude 2.1.309 cross-chain recovery

Status: **unreleased, partial compatibility work**. This document does not certify
the five reported private sources. Their new synthetic reproductions preserve
identifier relationships, but replace content, signatures and some enum values.

## Changes implemented in this branch

The preserved-context selector follows the native order: retirement/relink,
conversation-leaf selection, assistant-batch and parent-linked tool recovery,
then metadata descendants of the **original** leaf. Recovered records do not
create fabricated parent edges. Their positions and reasons are bound in the audit.

The source policy remains `claude-2.1.309-native-latest-preserved-v1` on unchanged
paths. The expanded paths use `claude-2.1.309-native-latest-preserved-v2`. Both
require an explicit OpenCode 2.2.5 destination; coverage remains UNKNOWN.

Persisted prompt/tool controls may be omitted only when the canonical selector
proves the native loader deleted them during retirement. Being physically before
a boundary, or absent from `selected_indices`, is not sufficient. Controls in the
loaded conversation remain unsupported.

The projection now compares the original source's visible-text/tool sequence with
the sequence produced by OpenCode's raw native messages. It checks preparation and
cold verification, independently of the portable representation that associates a
result with its call. Only separately validated `ImportedIncompleteTool` results
may be excluded as explicitly generated archival records.

This textual order contract refuses unproved reasoning/media/hosted-tool/native
window lowering, including media nested inside tool results. It also refuses the
material `toolUseResult.file.truncatedByTokenCap` override and content whose order
depends on Claude's tool-block normalization. A duplicate envelope cannot hide
the truncation override. The batch-order guard is deliberately conservative.

## Why tool pairs alone were insufficient

Consider this synthetic source sequence:

```text
call t2, call t1, result t1, result t2
```

The previous expected projection associated each result immediately with its
call. The writer and reverse parser agreed with that reordered expectation, even
though the native destination sent a different conversation to its model.

Stock OpenCode 2.2.5 uses the same assistant-content array to emit calls and then
results. Its ordinary transfer format has no independent result-position field:

| Representation | Effective order |
| --- | --- |
| Two assistant messages | call t2, result t2, call t1, result t1 |
| One assistant with tools t2, t1 | call t2, call t1, result t2, result t1 |

Neither preserves the example. Real Chat and Responses requests confirmed the
first ordering. The new guard rejects this input before importing a destination.
Textual batch recovery passed import, cold export, verification and continuation
through both adapters using a local scripted provider.

`compaction.native.messages` is more expressive, but has provider-compaction and
producer/model semantics. It is not a proven portable escape hatch. This branch
does not manufacture such a window or declare a false producer identity.

## Remaining source-control work

All five structural reproductions now reach the persisted-control guard. Each
still contains active prompt snapshots and nonempty deferred-tool records. The
selection fix therefore does **not** make those five sessions migratable.

Completing support requires a separate, reviewed contract for:

1. Selecting and validating the effective snapshot, render-point precedence and
   historical renderer options.
2. Rendering supported historical carriers with their original authority and
   causal order, including the effects of historical tool-registry records.
3. Separating historical tool declarations from actual destination tool/MCP
   availability. Migration must not activate historical permissions, hooks or
   queued commands.
4. Defining the treatment of the source system baseline. A chronological system
   message in OpenCode does not replace its initial runtime baseline. Treating
   source state as supplementary history must not be described as full runtime
   equivalence.

The existing fixtures are suitable for relationship tests, not for certifying
original signatures, all renderer semantics or complete request equivalence.

## Proposed destination work, not implemented

Strict preservation of the example requires a new destination transport contract,
such as independently ordered tool-result positions or an ordered event stream.
This is a proposal, **not an existing OpenCode field or endpoint**.

The implementation would need schema/import/export support, storage round trips,
model-request lowering, explicit capability/version negotiation and compatibility
tests. Unsupported stock clients must reject the new contract rather than silently
discard its ordering information. Grouping source assistant chunks must preserve
source batch identity and keep every call/result exactly once.

Acceptance requires source/target order agreement in actual Chat and Responses
requests, cold exports, normal human continuation, and no execution of historical
tools. System-baseline and renderer semantics need their own decision and proof;
an ordering extension alone does not solve them.

A less strict portable mode is a separate product choice: every runtime or ordering
difference would need an explicit loss description and review. It must not reuse
the claim of faithful native-context migration.

No OpenCode code, installation or running server is changed by this proposal.
