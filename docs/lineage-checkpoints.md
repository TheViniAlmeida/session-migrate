# Checkpoint primitives

Version 0.12.0.dev5 adds programmatic primitives for the next operation-integration
milestone. CLI/TUI transfers do not yet write checkpoints or assess continuity.
These helpers establish neither native preservation nor permission to update an
existing session.

## Exact semantic hashing

`canonical.hash_events(events, CanonicalPolicy())` hashes each ordered event,
including its payload, exact text, role, tool namespace/input/output/error and
block order. It avoids `Event` equality, which intentionally excludes payloads.
Native call identifiers resolve logical call ordinals; remapping those identifiers
does not change valid tool links. Duplicate IDs, orphan/ambiguous results and
unsettled calls produce fixed issue codes. JSON types, Unicode and order in
arrays remain exact. Unknown algorithm/policy versions are rejected.

Inline media positions hash decoded bytes and MIME. External media references
hash the reference and report a blocker, without fetching it or claiming a
content witness. Arbitrary tool arguments remain exact JSON, including fields
named `image_url`. Hashing portable events does not certify that a native parser
covered every semantic field.

`SemanticWitness` retains complete counts and chain roots. Its diagnostic arrays
are capped at1024; the authority covers every processed event and tool link.
`iter_canonical_units` exposes a stream for long-history comparisons.
`compare_prefix` recomputes the chain up to the prior count and compares both
unit and tool-link roots. It never searches text or matches a subsequence.
The prior policy must be supplied explicitly; an empty current stream also
requires its current policy. Its `sequence_extends` result describes only these
ordered sequences, not delta eligibility, native structure or a closed turn.

## Records and boundary metadata

`witnesses.SequenceWitness` holds an exact count and SHA256 chain root.
`PrefixWitness` schema2 holds independent units, links, optional structure,
closed-turn count and an explicit boundary. A boundary can be unproven,
native-closed or imported-closed. Constructors validate structural consistency;
they cannot establish native evidence. Zero pending tools or an assistant
message does not prove a completed turn.

Lineage source, expectation and checkpoint records accept optional v2 witnesses.
The expectation also accepts a relation hash for the future private projection
certificate. V2 uses an explicit record version and exact keysets. Existing v1
records reencode with the same bytes and hashes; unknown versions/fields fail
closed. No existing database migration or native-session update is performed.

## One capture, shared by parser and auditor

`native_snapshots.capture_source(SourceCaptureContext(format, path), byte_limit=...)`
reads a supported Claude, Codex or OpenCode file through a guarded descriptor.
A logical capture can use several bounded reads from that same descriptor.
Regular single-link files, named/open identities and ancestor identities are
checked around the read. Symlinks, FIFOs, oversized files and changed namespaces
are rejected by this capture API. Legacy path parser behavior remains separate.

`NativeSnapshot.from_bytes` binds an already captured export in immutable RAM,
without inventing a filesystem or native-store identity. `snapshot.parse()` and
`decoded_records()` consume those bytes and return fresh decoded objects.
Claude/Codex `parse_bytes`/`parse_records` and OpenCode
`parse_import_bytes`/`parse_session_bytes` preserve source provenance without
reopening it. The private snapshot JSON depth limit is128; total bytes are capped
at256MiB, and a caller may impose a smaller limit.

`check_source_identity` checks names and filesystem metadata only. Fresh content
verification requires another whole capture and its authority hash. Captures
have no public serializer and exclude their bodies and paths from repr. Native
coverage, expected/observed projection, namespace qualification and durable
operation integration require the subsequent adapter and operation gates.
