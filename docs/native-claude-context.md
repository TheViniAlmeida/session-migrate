# Native Claude context and editable OpenCode sessions

**Development version:** `0.12.0.dev18` · **Date:** 6 November 2026.

This release adds a closed reconstruction contract for supported Claude Code /
Desktop 2.1.309 compacted transcripts and a separate publication mode for
human-editable OpenCode 2.2.5 conversations. It does not certify every Desktop
transcript or reproduce private runtime state.

The five reported private compaction sources are **not certified by this work**.
Their complete files were not accessed. A metadata-only report contains many
carrier/control types; active-region and field-shape evidence is still needed.
Unsupported material records are refused, not silently omitted.

## Source contract

The reference is the actual stock 2.1.309 loader, not the SDK's display-history
getter. Official SDK 0.3.309 and CLI 2.1.309 native packages publish the same ELF:
`4d704a8aecbe038affa3000f498ae17c1b4f753add8facb8764120affc99b658`.
The SDK changelog documents the version parity. Their high-level history readers
still have different selection behavior.

The supported selector:

- Uses the last preserved `compact_boundary` and validates its summary anchor,
  including a path through supported intermediate metadata.
- Prefers `preservedMessages.uuids` when present; otherwise validates the
  `preservedSegment` parent walk. It preserves the declared native ordering.
- Treats `allUuids` as memory bookkeeping, not a requirement that every ID exist
  on disk and not an alternative source of conversation edges.
- Applies native relinks in a separate map. Source UUIDs, version fields, bytes,
  hashes, usage and physical occurrence indices are not rewritten.
- Does not blindly stop at a stale implicit `last-prompt` before a final reply.
- Refuses unresolved gaps, cycles, ambiguous checkpoints, unsupported batch/tool
  recovery, mixed identities and sidechains instead of using timestamp guesses.

The original file remains the complete physical record. The migrated stream is
its supported **effective context**, not an archival concatenation of everything
in that file. Old context retired by the native checkpoint is accounted for.
The original compact summary is retained as historical context; no replacement
summary is generated.

## Material carriers and authority

Carrier validation is shared by the parser and raw audit. It includes metadata
descendants that the native loader restores after the selected conversation leaf.

The initial closed contract admits:

- Existing supported conversation text/tool records, with guards against native
  delivery, error, batch and wire controls that change their meaning.
- Plain user `isMeta` text as typed context, not a fabricated human message.
- `instructions`, `context_sections` and `date` only with validated persisted
  plain-text `rendered` content and explicit `renderedRole: user`.
- Nonempty historical `system.local_command` text as context, never as a command
  to execute.
- The specifically tested inert shapes of `system.turn_duration` and
  `attachment.hook_system_message`, without rendering overrides.

The normalizer preserves text/block order and the supported native placement of
carriers relative to assistant and tool-result turns. It refuses material order
that depends on an unavailable native flag. It also refuses system-role promotion,
queued delivery, unsupported rendered alternatives, prompt snapshots, content
replacement, tool-registry controls and unknown active carriers/fields.

`rendered` is an engine rendering, not proof of the final historical request after
runtime hooks. Those hook transformations are not recoverable from this input
contract. They are not copied or recreated. Permission rules, credentials, hooks
and live MCP configuration are not migrated.

The old anonymized topology fixture used `synthetic`/`synthetic_metadata` labels.
It proves structural relationships but cannot certify the original carrier types.
Those labels are not a production whitelist. Tests with real schema shapes are
created separately with invented bodies and IDs.

## Destination and proof

The effective source context is emitted as regular portable conversation/tool
records and explicitly marked OpenCode `synthetic` context messages. The source
summary is context too: creating a new OpenCode compaction checkpoint here could
incorrectly remove instructions which Claude places before that summary.

This route requires exactly OpenCode **2.2.5**. Other targets do not receive a
silent reduced version of the reconstructed context.

The versioned policies are:

- `portable-native-context-225-v1`: strict tool linkage.
- `portable-native-context-225-archive-v1`: the existing reviewed-tree opt-in can
  represent a genuinely open call as `ImportedIncompleteTool`, preserving its
  ID, name and input with explicit losses.

Orphans, duplicate IDs/results, identity conflicts and unresolved causal links
remain errors. A target error is not a successful execution of the source call.
Raw marker checks, context-text hashes, complete unit/tool-link comparison and
source/target bindings remain required. A new target replay cut is not authorized
by a normalized text match.

Source coverage remains **UNKNOWN**. No completeness certificate, FULL claim,
delta eligibility or in-place update authority is granted. Native cold-recovery
gates are not broadened; uncertain imports are not repeated automatically.

## Human-editable trees

New tree operations default to `publication_mode: independent`:

```bash
smigrate tree review ... --publication-mode independent
smigrate tree migrate ... --publication-mode independent --acknowledge-partial
```

Each target is a normal root session without `parentID`. Original relationships
remain in the private proposal/journal/result through
`organizational_parent_target_id`; `parent_target_id` records the actual native
value. The SessionMigrate UI can display that external organization.

`--publication-mode native-subagent` is an explicit opt-in. It sets native
`parentID`, which disables direct human composing in the stock OpenCode UI.
This is not equivalent to editable organizational nesting.

Resume preserves the recorded mode. Old journals without a mode remain legacy
native-subagent operations; selecting a different mode cannot rewrite them.
Completed and uncertain nodes keep their existing guards.

The stock public `POST /api/session/:id/fork {}` can create a new root copy of a
native child. That is provenance from the existing OpenCode session, not proof
of a fork relationship between the original Claude sources. This release does
not alter, delete, archive or repair any existing user session automatically.

The stock `subagent` tool refuses unrelated root sessions. Independent human
prompting therefore does not establish native peer-to-peer messaging. A separate
coordination mechanism must be provisioned and validated independently.

## Interface changes

CLI and TUI offer the publication mode; the TUI's resume uses the mode from the
journal. The dashboard's existing `POST /api/trees/prepare` accepts optional
`publication_mode` with values `independent` or `native-subagent`; omission means
independent for a new operation. It is validated before work and bound to the
prepared capability. No new HTTP endpoint or web resume endpoint is introduced.

## Executed validation and limits

- The final selected integration suite passed 1,372 unique cases across 42 modules
  with Python 3.14.4 and locked pytest 9.1.1: no failures, errors or skips. Five
  stock-client cases were deselected for separate native runs. An earlier harness
  loaded system pytest 9.0.2 and produced seven fixture-registration errors;
  using the locked version resolved them without changing those tests.
- Lint, formatting, shell/JavaScript syntax and documentation links passed. The
  built wheel's 94 package files match the reviewed source, with a valid RECORD.
- Stock Claude 2.1.309 in normal mode, with private filesystem/network, fresh
  configuration and tools disabled, supplies the source-request reference.
  Earlier `--bare` probes are not evidence for carrier fidelity because that
  flag filters attachments.
- Stock OpenCode 2.2.5 Chat/Responses adapters consume the supported fixture,
  export after reopening, continue through a local scripted provider and execute
  one new synthetic MCP function. With MCP absent, historical evidence remains
  readable and no tool is executed. Historical commands never run automatically.
- These are real client/adapter and tool executions with deterministic local
  inference responses, not calls to a remote model or the operator's gateway.
- Actual stock UI tests send human prompts in independent roots and a public fork,
  preserve history and reject composing in a native child. Peer scope rejection
  is a separate negative test. Chrome's sandbox remains enabled.
- The real runtime's reported `Schema validation failed` was not reproduced by
  the synthetic stock matrices, including the 560-record candidate and controls
  applied through the API after import. Build, capabilities, plugins or other
  remaining differences must be examined without dumping private payloads.

The changes do not update existing installations, native frontends, MCP servers,
credentials or permission policies automatically.

## Pending runtime investigation

On 6 November 2026, the operator authorized deployment of the validated DEV18
contract while keeping the real-runtime incident as a follow-up. This does not
certify the five private sources or remove the reader's refusal checks.

The metadata report received that day confirms the same stock OpenCode 2.2.5
executable hash used by the gates and nine original user messages containing only
text, without structured files, agents or skills. It does not establish whether
the gateway received an HTTP request during the failed attempt. That question
needs correlated gateway metadata or a separate instrumented reproduction; a
generic error cannot prove either answer. No message bodies, attachment
names/paths, headers or credentials are needed. Active-region carrier shapes
remain a separate compatibility follow-up.
