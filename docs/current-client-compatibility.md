# Current client compatibility

DEV14 adds OpenCode **2.2.5**, Claude Code **2.1.317 / 2.1.318** and Codex
**0.166.0**, checked on 5 November 2026. The existing exact contracts below are
retained. OpenCode recognizes the 2.2 transfer schema, but native title and audit
proofs are pinned to 2.2.5. GitHub's generic OpenCode `releases/latest` still
points to the older 1.x series; the explicit 2.2.5 release is the target.

OpenCode 2.2.5 keeps the 2.1 public transfer shape, supports `time.busy`, and
moves some storage internally into blobs without exposing those details to the
migrator. The current checks cover CLI standalone and connected `--server`
import/export, authenticated API identity, session/message listing, cold reopen,
and an independently resumed persistent child with `parentID`. The importer
requires the parent to exist first. Omitting `parentID` from the list API includes
children; the ordinary CLI list returns roots only.

Codex 0.166.0 has per-content `content_item_metadata.provenance` in addition to
`content_item_kinds`. Typed provider context remains opaque rather than becoming
a user message. Literal user text is not filtered by a text-prefix guess.
Unknown provenance retains the audit's UNKNOWN classification. The native client
can read compressed `.jsonl.zst` rollouts, including inherited fork history;
DEV15 now reads compressed rollouts and resolves shared `history_base` dependencies
inside the selected root, with physical/composite hashes and strict bounded decoding.
The multi-component projection retains UNKNOWN coverage for ancestor private context.
See the [tree and storage guide](tree-migration.md) for decoder requirements and limits.

Claude 2.1.317/318 retains the historical portable writer contract. Independent
fork and cold-resume tests confirm that a CLI fork is a separate persistent
session, while its transcript does not provide a proven parent-session pointer.
Desktop nesting metadata requires a separate evidence-backed adapter. DEV15
implements persistent tree migration through explicit inventories and proven native
metadata, with an operator-declared manifest when a client does not expose its links.
See the [tree migration guide](tree-migration.md).

The `claude-nov`, `codex-nov` and `opencode-v22` CI selectors keep the October
selectors intact; the keyboard migration matrix also includes OpenCode 2.2.5.

November primary releases: [OpenCode 2.2.5](https://github.com/anomalyco/opencode/releases/tag/v2.2.5),
[Claude 2.1.318](https://github.com/anthropics/claude-code/releases/tag/v2.1.318),
[Codex 0.166.0](https://github.com/openai/codex/releases/tag/rust-v0.166.0).

## October contracts retained

DEV13 adds exact tested contracts for Claude Code **2.1.303 / 2.1.306** and
Codex **0.163.0**. The published stable versions checked on 21 October 2026
were Claude 2.1.306 and Codex 0.163.0. Historical Claude 2.1.209, Codex
0.144.4 and the Codex 0.153.4 paginated reader retain separate coverage.

The conservative writer defaults remain Claude 2.1.209 and Codex 0.144.4.
They describe the existing portable writer contract. Exact current destination
metadata can be requested through `--target-cli-version` or the TUI's target
CLI version field. Both historical and current headers were consumed by the
current native clients in isolated cold-resume tests. Codex still receives a
legacy-shaped artifact; its own clients may migrate stored history on paths
that perform migration. An `exec resume` in the native gate retained legacy
mode, so the migrator does not assume a rewrite always happens.

## Reviewed behavior

- Codex's provider-owned environment blocks are distinguished by the structured
  `content_item_kinds` metadata. They do not become user messages when a recent
  client appends to a legacy transcript. A user's literal text beginning with
  `<environment_context>` remains a user message without that metadata.
- A Claude selected ancestry that crosses a sidechain is refused, including a
  `last-prompt` reference into a child branch. A child conversation is not
  silently substituted for the primary conversation.
- Latest exact clients are registered consistently in source/target projection,
  schema-level USER emission and usage extraction. Codex usage supports the
  native legacy/paginated schema actually observed, preserving separate
  request, turn and cumulative counters. Cache writes use their explicit field;
  missing counters remain unknown.
- A Codex image backed only by a provider `file_id` is an explicit omission;
  the parser does not construct a fake or empty image URL.

Provider-owned request records, encrypted content, annotations, retained
context and unsupported compaction state retain their conservative audit/loss
gates. Mixed Claude client versions remain readable without acquiring a
single-client authority proof. Version recognition does not prove native
turn closure, in-place update, complete replay-state equivalence or billing.

## Claude custom project namespaces

Publication to Claude refuses a nonempty `CLAUDE_CODE_PROJECT_DIR_NAME` before
creating destination state. The stock 2.1.306 namespace probe returned success
and a model reply but persisted only a latch record in the selected namespace;
it did not establish resumable native history. That diagnostic remains separate
from the positive migration gates. Convert-only output does not publish to a
native namespace and remains available.

## Reproducible native gates

The native CI selectors `claude-current` and `codex-current` install exact
published packages and validate their ELF digests. Each runs six assigned
cases, with skipped assigned tests rejected. Historical selectors remain.
Tests create only disposable Git repositories and private HOME/XDG/client
stores, use explicit Haiku or fake model identifiers, and send inference only
to a loopback protocol stub in a private network namespace. No permission or
sandbox bypass flags are needed.

The gates cover fresh native capture, a second process resuming that capture,
writer artifacts with historical/current metadata, tool history, compaction,
unique canonical messages and ordinals, and Codex app-server listing/reading.
The application does not upgrade the operator's Claude/Codex installations.

Sources: [Claude 2.1.306 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.306),
[Codex 0.163.0 release](https://github.com/openai/codex/releases/tag/rust-v0.163.0),
and the [official OpenAI changelog](https://learn.chatgpt.com/docs/changelog).
The native format details were additionally checked against the installed
stock clients and the Codex tag commit `83086188ae0614bd882818ff1aa23f3194962012`.
