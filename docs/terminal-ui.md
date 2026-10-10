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
   new session; native title override and in-place update are unavailable.
5. Prepare the basic dry-run review: exact profiles, reserved target ID, record
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
designated stock OpenCode 2.0.23 executable. Its five permanent tests cover
three keyboard/core routes and two changed-source confirmation failures, with
isolated HOME/XDG, reserved identity, manifest, source witness and resize checks.
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
