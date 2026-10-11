# Offline USER handoff and return-new

The CLI and optional terminal interface use the same private handoff and
return-new runtime. Isolated native checks cover full/summary return and a new
persisted turn for Claude 2.1.209 and Codex 0.144.4. OpenCode 2.0.22/2.0.23
supports summary return with a DERIVED relationship and a PARTIAL destination
checkpoint: the accepted USER document, model/provider and project are verified,
while the native model variant remains an explicit context loss. In-place
update and native CAS remain unavailable.

## Current authority and explicit selection

Use an existing catalog and current saved profiles, or explicit current profile
ID/format/root fields. A source is qualified by its current profile, root and
native ID. Return-new requires an explicit completed parent operation whose
qualified target is that current source. Historical profile fingerprints and
roots do not reconstruct a current profile. Missing, ambiguous, stale or
contradictory context blocks preparation.

Event indices are zero-based and ranges inclusive. `--range FIRST:LAST` selects
literal event text; `FIRST:LAST:BLOCK` selects an actual provenance block index.
Repeat `--range` to select additional ranges; overlapping selections fail.
No clipped preview text is used. Unknown objective/decisions/state/verifications/
pending work/next step stay unknown until the user edits them. Tools, system
instructions and historical assistant messages are never replayed by a summary:
the accepted document becomes one explicitly marked USER handoff event.

```sh
smigrate --catalog /private/catalog.sqlite handoff review \
  --profiles /private/profiles.json --source-profile source \
  --source-native-id EXACT_SOURCE_ID --range 0:1 --json

smigrate --catalog /private/catalog.sqlite handoff export \
  --profiles /private/profiles.json --source-profile source \
  --source-native-id EXACT_SOURCE_ID --range 0:1 \
  --document /private/documents/new-handoff.md --json
```

Review prints content-free metadata. Export is an explicit document write,
never a target publication. Its parent directory must already exist, belong to
the user and have mode 0700. The new file must not exist; export creates an
owned single-link 0600 file exclusively. Both commands discard process-local
handles on exit. Their metadata cannot later be applied as authority. A new
command must recapture the actual source with current profiles and selection.

## Same-process manual review and summary

`handoff summary` exclusively exports the draft, then waits for a SHA256 line
on stdin. Manually edit the complete file in another terminal, preserving its
inode and private permissions. Calculate the digest of its whole UTF-8 content
and enter that digest in the waiting process. No editor, shell, provider or
resume command is launched. Changing/replacing/linking the file, malformed
UTF-8, an incorrect digest or changed original source blocks the attempt.
Documents/edits have a hard 1 MiB bound; excess fails without truncation.

```sh
smigrate --catalog /private/catalog.sqlite --state-home /private/migrator-state \
  handoff summary --profiles /private/profiles.json \
  --source-profile source --source-native-id EXACT_SOURCE_ID --range 0:1 \
  --edit-export /private/documents/new-reviewed.md --target-profile target \
  --cwd /absolute/project --target-cli-version 0.144.4 \
  --model-provider openai --model EXPLICIT_MODEL --json
```

By default this prepares metadata and discards the private prepared handle;
it writes no target or ledger. Add `--confirm` to publish the freshly prepared
handle once in that same process. Reusing proposal JSON, an exported receipt or
a handle from another process is unsupported. Source/body/options guards run
again through the runtime immediately before publication. Summary requires
explicit CWD, model, provider and an exact supported target release; the current
runtime supports Claude 2.1.209, Codex 0.144.4 and OpenCode 2.0.22/2.0.23 emission
subject to each adapter's actual prerequisites. A provider label in target
metadata does not make a provider request. This frontend has no model-provider
submission flow.

## Return to a new session

```sh
smigrate --catalog /private/catalog.sqlite --state-home /private/migrator-state \
  return-new --profiles /private/current-profiles.json \
  --source-profile current-b --source-native-id EXACT_CURRENT_B \
  --target-profile current-a --parent-operation-id EXACT_COMPLETED_PARENT \
  --content-mode full --cwd /absolute/project --target-cli-version 2.1.209 \
  --model-provider anthropic --model EXPLICIT_MODEL --json
```

Full return does not accept summary ranges or a handoff input. Summary return
uses `--content-mode summary`, explicit ranges and `--edit-export`, with the
same digest review described above. Both modes create a fresh A2 identity,
distinct from original A and current B, linked to the selected parent receipt.
The actual current B remains the native source authority. Default metadata
does not publish; `--confirm` consumes the newly prepared handle once.

Full return to OpenCode is blocked with `target_full_history_unproven` before
reading the parent or publishing. Its native importer adds model-variant context
that the full-history audit cannot certify. Choose summary return instead.
Summary verification accepts only the exact supported schema, literal USER
input and the single declared variant loss; other unknown fields, losses or
metadata changes block the operation. It never promotes native fidelity or the
destination checkpoint to VERIFIED. A variant change is not represented as
preserved source context.

Successful summary lineage uses a DERIVED relationship and a schema-2
assessment with a genuine schema-1 `baseline`, derivation and optional return
link. Its normal continuity result is UNTRACKED /
`derived_handoff_not_full_history`; it does not claim full-history equivalence,
NO_OP, delta, billing or native closure. Full operations retain existing wire
formats. Readers preserve the V2 wrapper rather than inventing a V1 comparison.

F5A CLI exits 0 for valid metadata/completion, 1 for a fixed operational failure,
and 2 for invalid arguments. `needs_reconcile`/`persistence_uncertain` report the
same operation ID, target and acknowledged evidence. Inspect that operation;
do not repeat publication. The separate recovery CLI accepts
`--handoff-document /private/documents/accepted.md` only as an input path for
the genuine runtime recovery guard, alongside the complete original options,
source/input/manifest and current profiles. A path or document does not issue a
receipt or prove publication.

## Terminal review

Choose **Handoff / return-new** or **Ctrl+H**. Select current profiles and the
qualified source explicitly, enter ranges, and capture. Edit the entire literal
document in the text area and confirm its SHA, or export and manually edit the
issued private file before accepting its SHA. Choose an explicit current
target, CWD/version/model/provider and, for return-new, the parent ID. Prepare
then confirm once. Full return skips handoff capture/acceptance.

The screen owns a single worker thread and private handles independently of
the ordinary transfer draft. Source edits discard its authority; edits after
review/preparation require new review. Back abandons a read whose native export
may finish in the background. Before publication it can cancel preparation or
commit. After the atomic publication barrier, Back, Quit and form changes keep
observing the same operation and receipt; they never kill a native job or claim
rollback. Resize preserves the proposal. Uncertain publication disables retry.
Results are metadata only and resume is manual.
