# Destination naming contracts

A destination title is independent of source title and identity. Every migration
still creates a new session; naming does not implement update or continuity.
`--title` continues to select native source metadata, while `--target-title`
requests the destination label. The source Session/title/path/bytes remain intact.

The app validates a nonempty string up to 1024 UTF-8 bytes, rejecting dangerous
terminal/bidi controls and invalid Unicode. It preserves valid Unicode, combining
characters, ZWJ and spaces exactly. It never truncates an override silently.

Native naming is verified only for the exact stock OpenCode 2.0.22 and 2.0.23
clients tested through public import, project-scoped list and separate cold
export. Evidence is from Linux x64 clients; it does not establish every platform,
custom build, future patch or harness. Other destinations use an explicitly
chosen local alias or reject native naming. No fallback is selected silently.

Local aliases are owned private metadata with format/canonical-root/native-ID
identity and root witnesses. The selected target profile's state environment
chooses the alias store. A profile rename does not change native identity; the
same UUID in another root does not share its alias. Aliases are exclusively
created after target identity verification, never written into native transcripts
or catalog titles and never used by the legacy source --title selector.
Read/display is an exact page/ref lookup, not a body index or global alias scan.

Title plans state intent. Receipts distinguish planned native/alias from observed
native or published alias. Convert-only remains pending; import/transfer verify
identity before publishing naming metadata. Manifests stay schema 2 and an
exclusive private schema 1 title receipt is separate. If native/file publication
succeeds and alias/title finalization fails, the operation needs reconciliation:
keep its identity/manifest/output, inspect manually, and do not autoimport/retry.
There is no distributed transaction or automatic rollback across native storage
and local metadata.

Proposals are schema 1 metadata, never executable artifact JSON. The core retains
one private single-use prepared handle; the TUI confirms that exact handle, while
CLI proposal discards it after printing. A proposal JSON cannot be replayed in
another process. Probe runtime effects are declared; proposal/dry-run does not
publish a session, artifact, manifest, alias or title receipt.

See [CLI flags](cli-reference.md) and [keyboard behavior](terminal-ui.md).
