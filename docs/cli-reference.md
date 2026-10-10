# CLI reference

This page documents the current `session-migrate` CLI. `smigrate` is an exact shorthand
for the same executable.

## Commands

```text
session-migrate inspect PATH [--format FORMAT] [--json]
session-migrate convert PATH --to TARGET --output PATH [OPTIONS]
session-migrate import PATH --to TARGET [--home PATH] [--dry-run] [OPTIONS]
session-migrate transfer SOURCE_ID --from FORMAT --to TARGET [OPTIONS]
session-migrate transfer --catalog-id ID --to TARGET [OPTIONS]
session-migrate transfer --title TITLE [--from FORMAT] --to TARGET [OPTIONS]
session-migrate proposal convert PATH --to TARGET --output PATH [OPTIONS]
session-migrate proposal import PATH --to TARGET [--home PATH] [OPTIONS]
session-migrate proposal transfer [SOURCE_ID | --catalog-id ID | --title TITLE] [OPTIONS]
session-migrate catalog refresh [ROOT OPTIONS] [--validate] [--json]
session-migrate catalog roots list|add|remove ...
session-migrate catalog list [FILTERS]
session-migrate catalog search QUERY [FILTERS]
session-migrate catalog show CATALOG_ID [--include-paths] [--json]
session-migrate lineage list [--limit N] [--offset N] [--json]
session-migrate lineage inspect --operation-id ID [--json]
session-migrate assess [--operation-id ID | QUALIFIED PAIR] [--profiles PATH] [--json]
session-migrate reconcile import|transfer [ORIGINAL OPTIONS] --operation-id ID --source-file PATH --input-file PATH [--confirm] [--json]
session-migrate adopt [QUALIFIED CURRENT PROFILES] --source-client-version VERSION --target-client-version VERSION [--confirm] [--json]
session-migrate lineage cancel-ready --operation-id ID [--confirm] [--json]
session-migrate lineage baseline --baseline-id ID [--json]
```

`FORMAT` and `TARGET` accept:

```text
claude  codex  pi  omp  opencode  copilot  antigravity  cursor
vibe  muse  qwen  kimi  grok  kilo  openhands  hermes  mastracode  devin
```

All eighteen formats are readable and writable. Cursor is an experimental,
text-only adapter pinned to one exact Cursor Agent build. Same-format migration
is supported as a portable rewrite into a new independent session.

## `inspect`

`inspect` reports structural metadata and counts without printing message text,
tool arguments/results, image bytes, or titles.

```bash
smigrate inspect ~/.codex/sessions/2026/08/20/rollout-...jsonl
smigrate inspect ./store.db --format cursor --json
```

It prints the source path, CWD, UUID, timestamps, SHA-256, and structural
counts. Those fields can still be sensitive. Successful inspection means the
container is structurally recognizable; conversion applies stricter semantic
validation.

`--format` bypasses automatic format selection. It does not make an unsupported
schema or version safe.

Kilo and legacy OpenCode official bundles intentionally share one schema and retain
stored metadata across cross-imports. A standalone bundle cannot be attributed
reliably, so automatic inspection fails closed; pass `--format kilo` or
`--format opencode` for a trusted legacy bundle. Flat OpenCode 2.0 bundles are
recognized distinctly; Kilo rejects that schema.

## `convert`

`convert` writes a standalone target artifact plus a sidecar manifest:

```bash
smigrate convert SOURCE --to codex --output ./rollout.jsonl
```

The manifest is `OUTPUT.session-migrate.json`. `convert` never installs into a
native agent home and never invokes a target CLI. For OpenCode and Kilo Code it
writes an official import bundle; for Antigravity and Cursor it writes a
complete SQLite database. Multi-file targets such as Vibe, Kimi, Grok, and
OpenHands use a validation bundle that `import` publishes into their complete
native layout. Hermes and Devin use bounded transaction bundles for their
shared databases; MastraCode uses a valid one-thread LibSQL database artifact.

## `import`

`import` converts and installs into a target store:

```bash
smigrate import SOURCE --to pi --cwd "$PWD" --dry-run
smigrate import SOURCE --to pi --cwd "$PWD"
```

The target paths are collision checked and never overwritten. `--dry-run`
performs conversion, native validation, and collision checks but does not
install a session or manifest. OpenCode's official read/list preflight can
initialize its ordinary XDG cache/database metadata during a dry run.

OpenCode and Kilo import always use their official CLIs and do not accept
`--home`; isolate or select them with normal `HOME`/XDG variables. Antigravity
and Cursor installs verify the exact pinned executable and its published
hashes. Muse and Qwen install one native JSONL. Kimi, Grok, and OpenHands
publish their validated multi-file native sessions together. Hermes delegates
the final transaction to the exact pinned `SessionDB.import_sessions` API;
MastraCode and Devin merge one validated identity transactionally into their
shared stores.

## `transfer`

Direct lookup uses a native source ID:

```bash
smigrate transfer SOURCE_UUID --from claude --to codex --cwd "$PWD"
smigrate transfer SOURCE_UUID --from omp --source-cwd "$PWD" --to codex
smigrate transfer SOURCE_UUID --from cursor --source-cwd "$PWD" --to claude
smigrate transfer SOURCE_UUID --from vibe --source-cwd "$PWD" --to codex
smigrate transfer SOURCE_UUID --from qwen --source-cwd "$PWD" --to kimi
smigrate transfer session_SOURCE_UUID --from kimi --source-cwd "$PWD" --to muse
smigrate transfer ses_... --from opencode --to pi --source-cli ~/.opencode/bin/opencode
smigrate transfer SESSION_UUID --from grok --source-cwd "$PWD" --to openhands
smigrate transfer ses_... --from kilo --to claude --source-cli /path/to/kilo
smigrate transfer SESSION_UUID --from openhands --to qwen
smigrate transfer 20260830_120000_a1b2c3 --from hermes --to codex
smigrate transfer SESSION_UUID --from mastracode --to pi
smigrate transfer fix-timeline-merging --from devin --to claude
```

Claude, Pi, OMP, Cursor, Vibe, Qwen, Kimi, and Grok can use `--source-cwd` to
select a workspace-specific store. OpenCode and Kilo are virtual: the supported
official CLI exports the requested ID. Hermes, MastraCode, and Devin are also
virtual catalog sources, but their selected native ID is parsed directly from
the agent's shared database. All other sources are read from their native files.

Catalog transfer avoids ambiguous paths and duplicate UUIDs:

```bash
smigrate catalog search "parser refactor"
smigrate transfer --catalog-id CATALOG_ID --to copilot
smigrate transfer --title "parser refactor" --from claude --to copilot
```

`SOURCE_ID` selects the source. `--session-id` assigns the new target UUID;
they are deliberately different concepts.

`--title` searches the existing catalog and proceeds only when one session
matches. An exact case-insensitive title wins over partial keyword matches.
Refresh the catalog first; if the title is ambiguous, use `catalog search` and
pass the selected opaque ID with `--catalog-id`.

Without `--to`, only Claude→Codex and Codex→Claude retain their historical
default. Every other source requires an explicit target.

## Conversion options

| Option | Meaning |
| --- | --- |
| `--format FORMAT` | Override source detection for file-based `inspect`, `convert`, or `import` |
| `--session-id UUID` | Assign a new target UUID; generated by default |
| `--cwd PATH` | Target working directory; precedence is option, source CWD, process CWD |
| `--target-cli-version VERSION` | Select legacy or 2.0 transfer schema for OpenCode; change metadata only for other targets |
| `--target-cli PATH` | Supported OpenCode or pinned Kilo, Antigravity, Cursor, or Hermes executable/source for native import |
| `--model-provider ID` | Codex, Pi, OMP, OpenCode, or Muse target provider |
| `--model ID` | Target model label for formats that persist one, including Grok, Kilo, and OpenHands |
| `--home PATH` | Target native home, except OpenCode and Kilo |
| `--dry-run` | Validate and collision-check without installing migrator artifacts |

An irrelevant target-specific option may be accepted but has no effect. The
manifest records the target metadata version and warns when it differs from the
validated writer release. Native OpenCode import selects its schema from the
installed CLI unless explicitly overridden: legacy `1.17.20` or the `2.0.x`
family. Conversion without native import defaults to legacy; use
`--target-cli-version 2.0.22` for a v2 bundle. Other native schema series fail
closed. See [OpenCode v2](opencode-v2.md) for exact validation scope. Kilo,
Antigravity, Cursor, and Hermes installation requires the exact pinned version.

## Home resolution

| Format | Default source/target root |
| --- | --- |
| Claude | `$CLAUDE_CONFIG_DIR`, otherwise `~/.claude` |
| Codex | `$CODEX_HOME`, otherwise `~/.codex` |
| Pi | `$PI_CODING_AGENT_DIR`, otherwise `~/.pi/agent` |
| Oh My Pi | `$PI_CODING_AGENT_DIR`, otherwise `~/.omp/agent` |
| OpenCode | official CLI under its normal XDG data root |
| Copilot | `$COPILOT_HOME`, otherwise `~/.copilot` |
| Antigravity | `~/.gemini/antigravity-cli` |
| Cursor | `$CURSOR_CONFIG_DIR`, `$XDG_CONFIG_HOME/cursor`, otherwise `~/.cursor` |
| Vibe | `$VIBE_HOME`, otherwise `~/.vibe` |
| Muse | `$XDG_DATA_HOME/muse`, otherwise `~/.local/share/muse` |
| Qwen | `$QWEN_HOME`, otherwise `~/.qwen` |
| Kimi | `$KIMI_CODE_HOME`, otherwise `~/.kimi-code` |
| Grok | `$GROK_HOME`, otherwise `~/.grok` |
| Kilo Code | official CLI under its normal XDG data root |
| OpenHands | `$OPENHANDS_CONVERSATIONS_DIR`, otherwise `~/.openhands/conversations` |
| Hermes Agent | `$HERMES_HOME`, otherwise `~/.hermes` |
| MastraCode | `$MASTRA_DB_PATH`; otherwise `$MASTRA_APP_DATA_DIR/mastra.db` or the platform data directory |
| Devin CLI | `$XDG_DATA_HOME/devin/cli`, otherwise `~/.local/share/devin/cli` on Linux |

Explicit `--home` or `--source-home` wins where supported. All CLI path options
expand `~` consistently.

`convert`, `import`, and `transfer` delegate to the same typed operation core.
Their options, JSON fields, collision behavior, and exit codes remain unchanged.
The Python API accepts an ephemeral environment mapping per operation; the CLI
uses a snapshot of its normal process environment and adds no profile flag.
Stage callbacks emit no transcript data and do not change CLI output. Confirmed
native import followed by verification or manifest failure raises a typed
reconciliation error; the CLI still prints a content-safe error and exits 2.
MastraCode's `MASTRA_DB_PATH` must end in the canonical `mastra.db` filename;
custom filenames fail before the migrator opens or creates a sibling store.
Hermes refusal errors omit free-form native diagnostics to keep imported
conversation text and credentials out of CLI errors.
Catalog-selected OpenCode/Kilo roots must map to the same directory store the
official CLI reaches through `XDG_DATA_HOME/opencode` or `XDG_DATA_HOME/kilo`.
Safe directory aliases work; unmappable roots fail before native export. A
matching session ID or hardlinked database in another directory is insufficient.

## Catalog

The private SQLite catalog is at
`$SESSION_MIGRATE_CATALOG`, otherwise
`$XDG_STATE_HOME/session-migrate/catalog.sqlite3`, otherwise
`~/.local/state/session-migrate/catalog.sqlite3`.

Refresh auto-registers existing default, environment-selected, and ancestor
project roots:

```bash
smigrate catalog refresh
smigrate catalog refresh --discover-under ~/dev --validate
```

Additional roots are repeatable:

```text
--claude-root PATH       --codex-root PATH
--pi-root PATH           --omp-root PATH
--opencode-root PATH
--copilot-root PATH      --antigravity-root PATH
--cursor-root PATH       --vibe-root PATH
--muse-root PATH         --qwen-root PATH
--kimi-root PATH
--grok-root PATH         --kilo-root PATH
--openhands-root PATH
--hermes-root PATH       --mastracode-root PATH
--devin-root PATH
```

`--discover-under` is bounded to the supplied directory, never follows
symlinked directories, and recognizes conventional hidden stores. Arbitrary
custom root names must be registered explicitly. “All sessions” means all
recognized entries in these configured/discovered roots—not a whole-disk scan.

Search is case-insensitive across native title/name metadata and IDs. Multiple
keywords are ANDed and may occur in any order:

```bash
smigrate catalog search "database migration" --format codex
smigrate catalog search "timeout postgres" --lifecycle archived
smigrate catalog list --status candidate --since 2026-08-01T00:00:00Z
```

Paths and CWDs are neither searched nor printed unless `--include-paths` is
passed. The catalog never stores conversation bodies or tool payloads. See the
[catalog guide](session-catalog.md) for statuses and schema behavior.

## Successful JSON output

`convert`, `import`, and `transfer` print one content-free JSON object:

```json
{
  "cwd": "/target/workspace",
  "dropped_events": {"thinking:unsupported": 2},
  "dry_run": true,
  "manifest": "/target/state/session-migrate/manifests/UUID.json",
  "output": "/target/native/session/path",
  "records": 12,
  "session_id": "TARGET_ID",
  "sha256": "TARGET_SHA256",
  "source_format": "claude",
  "target_format": "cursor",
  "warnings": []
}
```

The historical field name `dropped_events` includes omissions,
transformations, and retained inconsistencies that need operator attention.
Non-empty warnings do not mean the command failed. Review them before resume.

Manifests use schema version 2 and include migration version, source/target
identity and hashes, structural counts, warning objects, and the same loss
counters. They never include message/tool/media bodies, but their paths, IDs,
CWDs, timestamps, and hashes are operationally sensitive.

A dry-run conversion is regenerated on apply. A fixed `--session-id` usually
pins native paths, but generated structural IDs/timestamps can change, so do not
expect identical SHA-256 values.

## Exit behavior

- `0`: success, help, or version
- `2`: argument error or expected migration failure

Expected failures are printed to stderr as:

```text
session-migrate: error: MESSAGE
```

There is no overwrite/force mode. Source files are always left untouched. If
manifest finalization fails after an external/native install, the error states
that the target session may already exist so the operator can inspect it before
retrying.

## Optional terminal interface

`session-migrate tui` opens the keyboard interface only when explicitly selected.
Use the existing global `--catalog PATH` and optional `tui --profiles PATH` to
select private state. It requires an interactive terminal and the explicitly
installed `session-migrate[tui]` extra; missing prerequisites return exit code 2
without automatic installation. See [Terminal interface](terminal-ui.md) for
profile selection, bounded previews, confirmation and cancellation behavior.


## Destination titles

`--target-title TITLE` requests a destination name independently of the source
`--title` selector. Names are preserved exactly, including spaces, Unicode and
ZWJ; empty/whitespace-only names, terminal/bidi controls and names exceeding the
app's 1024-byte UTF-8 limit are rejected. No silent normalization or truncation
occurs. Omitting the flag preserves the existing conversion behavior.

Native naming currently has exact-client proof for stock OpenCode **2.0.22**
and **2.0.23**, through its public session list and cold export. This does not
promote other builds, versions or harnesses. Unsupported native naming fails;
choose `--local-title-alias` explicitly if a private app label is wanted.
Aliases do not rename a native picker or change catalog/source title matching.

```sh
smigrate import SOURCE --to opencode --target-cli /path/to/opencode \
  --cwd /absolute/project --target-title "Ação 🧪" --require-native-title
smigrate import SOURCE --to codex --home /absolute/native-home \
  --cwd /absolute/project --target-title "Local label" --local-title-alias
```

`--require-native-title` and `--local-title-alias` are mutually exclusive and
require an explicit title. Convert-only refuses the native requirement because
it cannot verify an installed name. A titled standalone conversion records
`pending_native_import` or `alias_pending`, never installed persistence.

With title flags, successful output is a **schema 1 receipt** containing the
unchanged `legacy_result`, requested/native/local naming metadata, verification,
identity and probe effects. Native/session manifests remain **schema 2**. A
private, exclusive title receipt is separate from the manifest. Alias publication
follows confirmed target identity; neither aliases nor title receipts are
published by a dry-run. Alias/receipt failure after publication returns exit 2
with content-free `needs_reconcile` JSON on stderr: target ID, phase, manifest,
root/output and a manual inspection action. Do not repeat the publication.
Without title flags, legacy result JSON and manifests are unchanged.

See [title contracts](target-titles.md) for identity and verification limits.

## `proposal` — read-only review metadata

`smigrate proposal <convert|import|transfer>` uses the same selectors/options as
its direct counterpart and prints a **schema 1 proposal** without prompting.
It prepares one artifact, projects only metadata, then discards the private
handle at process exit. It never publishes a session, output, manifest, alias or
title receipt. Official native read/list/export probes may initialize normal
cache/store/WAL metadata; the proposal's `effects` declare those runtime effects.

```sh
smigrate proposal convert SOURCE --to codex --output /absolute/out.jsonl
smigrate proposal transfer --catalog-id CATALOG_ID --to opencode \
  --target-cli /path/to/opencode --cwd /absolute/project --target-title "Review 🧪"
```

The proposal includes root-qualified source identity, counts/read scope,
destination/schema/client version, CWD/model/provider, reserved UUID/native ID,
artifact SHA256, losses/warnings, title intent and preflight/effects. It contains
no conversation bodies, tool arguments/results, media bytes or full environment.
Names and paths are still private metadata; protect any saved JSON accordingly.

Saved proposal JSON is informational. There is no `--apply`, confirm or
cross-process replay. Use the TUI or the core API in one process to publish the
same prepared artifact. Direct `convert`/`import`/`transfer` commands continue to
execute normally; they do not apply a previous proposal JSON.

## Continuity

`lineage list`, `lineage inspect` and `assess` consume existing private metadata
and current root-qualified profiles. They do not import, update or resume a
session. See [continuity and recovery](continuity.md) for selection, profile
bindings, read effects, status meanings and explicit limits.
