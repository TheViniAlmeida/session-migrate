# Interactive terminal migration

The optional terminal interface uses Textual 8.2.8. The converter core continues
to require only the Python standard library. Install the `tui` extra explicitly
in your chosen environment, then run:

```sh
smigrate --catalog /absolute/private/catalog.sqlite3 tui
smigrate --catalog /absolute/private/catalog.sqlite3 tui --profiles /absolute/private/profiles.json
```

An interactive stdin/stdout terminal is required. A missing terminal or extra
returns exit code 2 with guidance; nothing is installed automatically. Calling
`smigrate` without arguments retains the existing CLI behavior.

Use Tab/Shift+Tab to move, arrows to choose profiles/sessions and Enter to activate
buttons. Escape returns to the previous step and preserves form inputs. Ctrl+Q
quits when an import is not running. Terminal resizing and SSH terminals are
supported. Register source roots with the existing catalog CLI before listing;
the interface does not recursively discover or automatically refresh stores.

1. Choose a registered source root or read a private profile file. Confirm its
   format, native root, HOME and CLI. HOME is separate from the project CWD.
   OpenCode/Kilo require an explicit executable and their exact native store.
2. Search title/native ID/project metadata and filter availability, lifecycle
   or native start date. RFC-3339 dates include a timezone. Pages contain at most
   50 entries. Missing/unsupported rows remain visible with their reason.
3. Open only the selected session's preview. Messages are literal text, tools
   begin folded, and media is metadata only. Page controls and tool folding are
   visual options; they never modify the transferred history or fetch media.
4. Choose a destination profile/root, HOME, CLI and absolute project CWD.
   When required, supply provider, model and target CLI version. Capabilities
   describe supported output and media restrictions. Every transfer creates a
   new session. Optional native titles require exact-client support; app-only
   aliases require an explicit choice. In-place update remains unavailable.
5. Prepare the metadata review: exact profiles, reserved target ID, record
   count, reported losses/warning codes and effects. Native OpenCode/Kilo probes
   can initialize normal cache/store state. Confirm once to install. Changing
   any option invalidates preparation and requires preparing again.
6. The result shows the effective target ID, output, manifest and a literal
   manual-resume instruction. The interface never runs the resume command.

Profiles are ephemeral unless the user explicitly chooses **Save new private
file**. That action saves the source profile using the existing exclusive JSON
snapshot contract. Existing files are never replaced. Read requires an owned
0700 storage directory and owned single-link 0600 regular file; symlinks and
unknown schemas are rejected. Profile edit/removal is not provided. See
[profile contracts](session-profiles.md).

## Responsiveness, cancellation and limits

Catalog connections open in their worker thread. One operation job and one
preview reader may run at once; no unbounded queue is accumulated. Preview
snapshots use the bounded in-memory cache and explicit event/text/page limits.
Errors/progress omit native exception bodies and tracebacks.

Interactive reads allow at most **8 MiB** for the selected physical source or
native export. OpenCode/Kilo apply the cap to the selected session export before
parsing; their global database size does not block a small session. Other shared
databases conservatively count the database, WAL/SHM and known companion metadata,
so a large shared database may block a small session's preview. The CLI remains
available for larger sources. History is never truncated
to fit the preview. Physical sources, including Grok/OpenHands directories,
use a bounded immutable private copy of only the adapter's authoritative files;
filenames/layout, original source path, hash and provenance are preserved.
Kimi snapshots retain the adapter's absolute `homedir` identity check against
the original session directory. Its existing relative `homedir` metadata policy
is unchanged: those values do not select files for this reader; only the fixed
`agents/main/wire.jsonl` and session `state.json` paths are read.
Directory enumeration is capped and does not recurse into unrelated sessions.
For Hermes/MastraCode/Devin shared databases, 8 MiB is a conservative metadata
preflight, not an atomic selected-session read cap: their existing adapter
safety limits govern growth during a concurrent read. No additional database
backup of unselected conversations is introduced. Existing adapters retain their export timeouts/safety caps;
a Python thread cannot forcibly stop a blocked adapter. Cancelled or timed-out
preview readers keep their bounded slot until they finish.

Cache/draft validity includes root-qualified identity and profile options.
Physical sources use device/inode/size/nanosecond mtime/ctime, including SQLite
WAL/SHM. OpenCode/Kilo use a hash of the selected authoritative export, re-read
before cache reuse, review and publication; their exporter can change WAL/SHM
without changing session history. Store metadata still guards identity and
availability. A source/options change invalidates the review, including a check
after preflight immediately before publication.
The native adapters re-read/revalidate identity during actual transfer. These
checks do not lock an external native client; a source can still change during
a later operation, and existing adapter validation remains authoritative.

Cancellation uses an explicit atomic token, independent of progress observers.
The core claims the token when `run_operation` begins; profile validation that
fails before entering the core has not consumed an operation. The interface
discards that attempt's token on its error result. Escape may cancel before
loading, after parsing or after preflight, up to the publication boundary. After installation starts, Escape/Ctrl+Q keeps observing
verification; it never kills the importer or claims rollback. A native import
that succeeded but failed local verification displays its target ID and asks
for manual attention, without automatic retry.

## Validation and metadata benchmark

Headless tests use Textual Pilot with stdlib `asyncio.run`, synthetic temporary
homes and fake native adapters. They cover keyboard Claude→Codex,
Codex→OpenCode 2.0 and OpenCode 2.0→Claude, reserved ID parity, resize, slow
readers, safe literal projection, cancellation boundaries, changed source/options,
exclusive profile storage and no duplicate import. The optional-extra CI job
runs on Python 3.11–3.13 and rejects skips. The core job remains without the extra.

A separate `tui-native` job runs Python 3.13 with the extra and the explicitly
designated stock OpenCode 2.0.23 executable. Its seven permanent tests cover
three keyboard/core routes, two changed-source confirmation failures and two
title-policy routes, with isolated HOME/XDG, reserved identity, manifest, source
witness and resize checks.
OpenCode endpoints use real official import/export in fresh processes without
warming or provider calls. Claude/Codex outputs are parsed in this module; their
native cold-resume/append evidence comes from the existing exact-client CI gates.
Parser success alone is not claimed as proof of native resumption. Missing
prerequisites cause opt-in skips outside this job; its JUnit gate rejects skips.

The pagination benchmark creates 10,000 synthetic metadata entries and reads
two pages of 50 without loading any session body. Its measured duration is
recorded with the validation result; wall-clock timing is not a test threshold.
On 10 October 2026, the local Python 3.14.4 validation environment measured
**0.051342 seconds** for the two pages. This is a local measurement, not an
estimate of another machine's performance.


## Destination titles and exact proposals

The destination form has an optional title, **Use an explicit local alias** and
**Require verified installed native title**. An exactly empty input means no
override; whitespace-only input is an error. Valid spaces and Unicode remain
exact. Native and alias requirements are incompatible. Unsupported native
naming never checks the alias box automatically: choose the app-only alias
explicitly or leave the title absent. The capability panel describes exact
client/version naming support; preparation checks the effective client.

Review displays source native title/counts/read scope, the effective source and
target HOME/XDG selectors, root/CLI/schema/version, CWD/model/provider, reserved
ID, exact artifact SHA256, title intent, losses/warnings and actual probe effects.
The controller serializes once and retains one private prepared handle. Confirm
consumes that handle and publishes the same bytes/ID. Back or an accepted option
edit discards the handle and disables confirmation while preserving inputs;
prepare a new proposal explicitly. Resize never regenerates the proposal.

Requested, planned native, observed native and local alias labels are separate.
`pending_native_import` and `alias_pending` are intent, not saved/verified names.
`native_verified` requires native observation; `local_alias`/`published` means a
private app label, never a picker rename. Results show the separate title receipt
and the existing manifest. Native manifests remain schema 2. Session lists show
native title and local alias in separate fields; aliases are looked up only for
that page's exact profile/root/ref, do not replace catalog titles and do not
change native-title search.

Cancellation is decided atomically before invalidating generation/draft. A
commit that has entered publication keeps observing its original receipt even
if an input event already changed the form. Closed preparation tokens still
allow option changes. Escape/Ctrl+Q after publication keeps waiting for
verification; neither action kills the importer or claims cancellation. A
partial native/file publication reports identity, phase, manifest/output and
manual inspection, blocks retry navigation and does not autoimport again.
Resume instructions remain literal, quoted, profile-qualified text, never an
executed action. All new labels use the existing literal widgets; no history or
media URI is interpreted or fetched.

Two additional exact-client keyboard cases cover stock OpenCode title
verification and an explicit Codex alias with separate source/target homes.
Their commands use bubblewrap with network and host homes/storage hidden;
missing prerequisites fail the designated no-skips gate. No model or provider
continuation is asserted by those cases. Existing keyboard/native routes and
source-drift checks remain part of the same job.

## Durable import state

Real transfers persist publication intent before invoking the native installer
and observe an acknowledged destination before completing the record. During
intent recording, Escape cannot cancel an operation that has crossed the gate.
An uncertain result retains its operation ID, reserved target and evidence
availability for inspection; it does not offer an automatic retry. A reserved
target does not itself prove installation. See
[migration records](migration-records.md) for the three native-result states and
the separate migrator state directory.

## Browse operation continuity

Choose **Operations** or press **Ctrl+O** to open the separate operation browser.
It lists durable metadata in pages of 50 and accepts an explicit operation ID.
**Inspect** reads recorded metadata; **Assess** reads current authoritative
endpoints through the shared continuity core. Both run in a separate worker.
The screen displays durable/effective native state, qualified roots and IDs,
coverage, fidelity, comparison reasons and declared native read effects.

Assessment uses an explicitly selected private profile file, the actual profiles
retained from a completed transfer in this application session, or current
registered catalog-root profiles. It does not construct a profile from a
historical hash or receipt path. Missing context is shown as unavailable.
A missing or stale target catalog entry requires an explicit catalog refresh;
this browser does not register roots or refresh the catalog automatically.

**Cancel read** abandons the result; a native export already running can finish.
It does not cancel a transfer publication or invalidate its prepared handle.
**Back** returns to the previous migration step. The browser offers inspection
and assessment only; reconciliation and adoption use separately confirmed
core/CLI operations. A delta label does not enable update: native turn closure
and external-client coordination remain unproven. See
[continuity and recovery](continuity.md) for the exact limits.
