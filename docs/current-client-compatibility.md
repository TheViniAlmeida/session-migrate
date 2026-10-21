# Current Claude Code and Codex compatibility

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
