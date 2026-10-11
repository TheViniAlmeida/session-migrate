# Local web dashboard

The dashboard is an optional local interface for explicitly selected saved
profiles. Its API uses the same prepared-operation and commit core as the CLI.
It does not start a service automatically.

Install the `web` extra, then select existing profiles and a catalog:

```bash
pip install 'session-migrate[web]'
smigrate --catalog /absolute/catalog.sqlite3 web \
  --profiles /absolute/private/profiles.json \
  --profile source-profile --profile destination-profile \
  --bootstrap /absolute/private/launch.json
```

The profiles and bootstrap parent use the existing private ownership/mode
contract. The bootstrap path must not already exist. The command prints the
local URL and file path. Open that URL and use the page's file picker to load the
launch file. Tokens never appear in a URL, terminal output, HTML, browser storage
or application logs. The connection lasts at most thirty minutes.

The default scope is readonly. Add `--allow-publish` to enable explicitly
reviewed transfers for the selected profiles. `--project NAME=/absolute/path`
adds a project choice at launch; handoff and return-new require an explicit project; HTTP cannot supply arbitrary roots, HOME,
environment variables, CLI paths or files. `--port 0` selects an available
loopback port; an explicit port must be between 1024 and 65535. The listener
always binds to `127.0.0.1`. Forwarded headers are disabled.

## Transfer and operations

Select a source profile/session, inspect its literal bounded preview, choose a
destination and prepare a review. A review owns a private in-process handle;
JSON does not recreate it. Confirmation transfers that handle to a single
worker. Repeated confirmation with the same key returns the same job, including
when its original review has expired or has already been consumed.

Native confirmation uses the core's actual publication token. Before publication it
can cancel queued work or discard an unconsumed review. After publication starts,
cancel, logout and shutdown observe the original operation through completion;
they do not interrupt the native writer or automatically retry an import.
An uncertain result remains inspectable by its operation ID.

Operation inspection reads the base migration state home, independently of the
target profile environment. The dashboard filters records to the selected
current profile namespaces and fingerprints. Readonly inspection does not
initialize a ledger or refresh/migrate the catalog. Refresh the catalog through
the ordinary CLI when required.

## Usage observations

Usage is extracted from an independently captured native source. Counter fields
are observations, not a bill or proof of consumption after migration. Missing,
invalid or ambiguous dimensions remain unknown. Cache/reasoning aliases and
cumulative observations are not silently added together. Pagination retains the
same capture revision and global classification; a changed or expired capture
requires refresh.

The current registry does not prove native additivity, stream finality, epochs
or historical imported accounting. Native aggregate totals and costs remain
unknown. An adopted baseline and a writer's synthetic zeros do not establish an
imported prefix. Context percentage requires a reliable matching numerator and
window; without them it remains unknown.

## Connection lifetime

Disconnect revokes the server run and clears browser memory. Closing a browser
tab alone does not prove server revocation. Expiry, stopping the process and
restart invalidate the run. A restarted process cannot revive private proposal
handles or transient job state; durable metadata remains available through the
ordinary lineage workflow.

## Handoff, return and explicit services

`--allow-handoff` enables the editable handoff workflow for this run.
`--allow-return-new` enables full or handoff return to a new session. Both flags
require `--allow-publish`. Choose source event indices (zero-based, inclusive),
the destination client version/model/provider and an explicitly selected project.
A captured handoff retains its source binding while you edit the complete text.
Accepting the text prepares a proposal; publication still requires confirmation.
The exact UTF-8 document and its SHA-256 are checked without trimming or clipping.
Drafts expire after five minutes and remain private to their authenticated run.
Changing the source or options requires a fresh capture.

A return selects a completed qualified parent transfer and uses the current
source session, including work performed after the original migration. It creates
a different destination identity. Full return to OpenCode remains unavailable;
OpenCode 2.0.22/2.0.23 user handoffs retain PARTIAL target coverage because the
importer adds a model variant. A handoff is derived context, not full-history
continuity. See [handoff and return](handoff-return-new.md) for exact client proofs.

`--allow-adopt`, `--allow-reconcile` and `--allow-cancel-ready` separately enable
reviewed service actions and each requires `--allow-publish`. Adoption records a
current baseline without importing messages or proving historical continuity.
Reconciliation needs the original private context retained by this launch and
its original files; the browser cannot reconstruct it from operation metadata.
Cancel-ready binds the current operation record and store identity, and only
cancels an operation that has not started native publication.

A service job reports `service_commit_entered` once effects become possible;
this is distinct from native publication or acknowledgment. After that point,
revocation blocks new requests but the owning worker completes or reports the
same subject as uncertain. The dashboard does not pretend that an entered
service or native import has been cancelled.

Editable documents live only in the process. An uncertain handoff publication
may require the original document and the CLI recovery workflow; restarting the
web process does not recover that private context automatically.

Assets are packaged HTML/CSS/JavaScript, with no CDN or browser build tooling at
runtime. Native update remains unavailable until its separate concurrency and
recovery proof passes. There is no vendor-model submission from the dashboard.
