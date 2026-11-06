# Reviewed portability

Base version: **0.12.0.dev19**. The operator selected this contract on 6 November 2026.
Inline PNG support below was integrated in **0.12.0.dev20**.
It is separate from strict native-context migration.

## Contract

Reviewed portability preserves the selected conversation while describing the
runtime and ordering differences required by the destination **before publication**.
The initial source is Claude 2.1.309 with a proven preserved-compaction selection;
the destination is exactly OpenCode 2.2.5. Unsupported sources do not fall back to
this mode automatically.

The reader retains user/assistant text, tool identifiers, names, displayed inputs,
results and error status. It uses the validated conversation graph and recovery
rules; it does not concatenate unrelated branches or invent missing ancestors.

Persisted thinking and selected historical carriers remain visible as quoted
USER DATA, including their textual variants and cached content. Original SYSTEM
authority is described rather than recreated. References without embedded content
remain references; the migrator does not read their paths. Known tool-reference
result blocks become explicit historical text with the original tool name.

Prompt snapshots, deferred-tool state and other runtime bookkeeping are retained
in a private archive of the **complete original source capture**. They do not
configure the destination's tools, credentials, hooks, permissions or queues.
Displayed tool inputs remain the conversation input; alternative wire inputs are
archived and their differences are reported. This is not a claim about the source
runtime's actual executed input.

The preview distinguishes:

- Selected conversation and quoted content that will be visible in the destination.
- Runtime records retained in the private archive but not restored as runtime state.
- Changed authority, representation and tool-result placement.
- Excluded/retired history and external references without an embedded body.

Unknown content, ambiguous identities, conflicting duplicates, orphan or duplicate
tool results and unsupported media are not licensed for silent omission. An open
tool call remains rejected by the normal policy. The existing explicitly reviewed
tree archival policy may instead generate `ImportedIncompleteTool`, with its own
loss acknowledgement; opting into reviewed portability does not enable that policy.

## Review and publication

`reviewed_portability` selects a contract; it is **not** confirmation.

1. Capture the source through the bounded reader and prepare an immutable proposal.
2. Show the conversation/runtime distinction, exact declared differences and planned
   archive metadata. Audit the source independently of the writer.
3. Obtain a fresh confirmation acknowledging the displayed reviewed-portability
   differences. Changed inputs, options or proposal data require a new review.
4. Write and verify the original capture in a private operation archive before
   native publication. Files use mode 0600 and directories 0700; writes cannot
   overwrite an unrelated file or follow a symlink.
5. Import the destination and verify its cold export. Its content and effective
   ordering must match the reviewed artifact, not an undeclared transformation.

If the archive cannot be written or verified, publication stops. If publication
becomes uncertain, retain the archive and evidence; do not automatically reimport
or roll back an existing destination.

Coverage remains PARTIAL/UNKNOWN as justified by the raw audit. Human confirmation
does not produce a completeness certificate, FULL lineage edge, delta authority or
proof of native runtime equivalence. Public cold recovery for this OpenCode route
is not introduced by the new mode.

## Interfaces

The integration uses these names:

```bash
smigrate proposal transfer SOURCE_ID --from claude --to opencode \
  --target-cli-version 2.2.5 --reviewed-portability

smigrate transfer SOURCE_ID --from claude --to opencode \
  --target-cli-version 2.2.5 --reviewed-portability
```

The second command shows its proposal and requests confirmation. For a deliberate
noninteractive confirmation, `--acknowledge-reviewed-portability` explicitly
accepts the displayed differences. It does not disable audit or mutation checks.
`--dry-run` and `proposal` never publish.

Tree preparation accepts `--reviewed-portability` independently of
`--publication-mode`. It still requires `--acknowledge-partial` where applicable
and a fresh confirmation. Resume preserves the journal's original portability
mode; changing it is rejected. Completed nodes are not imported again.

The TUI exposes the opt-in before source preview. The dashboard extends its
existing preparation and confirmation routes with boolean
`reviewed_portability` and `acknowledge_reviewed_portability` fields respectively;
tree confirmation retains its existing partial-acknowledgement contract. No new
endpoint is required. Preview caches and asynchronous responses must distinguish
strict from reviewed reads.

Interactive reviewed reads have a bounded 64 MiB default budget; strict reads keep
their existing budget. An explicitly configured lower budget must still be honored.
Preview text may be paginated or limited for display; that does not authorize
truncating the transferred conversation or original archive.

Reviewed web tree preparation allows up to 300 seconds, with cancellation still
available. Existing strict/service/handoff preparation limits are unchanged.

The DEV19 unitary API has been independently validated and released. A read-only
diagnostic snapshot is not authorization to publish a migration or bypass review
of a real source. Existing and already-functional sessions are preserved; later
improvements are tested in new candidates or explicitly authorized copies.

## Inline PNG tool results — DEV20

The media extension applies only when the reviewed source contains supported
inline PNG images in completed, non-error tool results. It uses a separate exact
source selection and projection policy; text-only DEV19 inputs retain their
existing contract.

Image bytes, MIME, displayed tool input, identifier and native history association
must be preserved. The source capture is still archived privately. Invalid data,
unsupported formats, indexed-color/animated PNG and error results containing images are refused;
there is no silent image removal, resizing or replacement with a text marker.

The preview must disclose a protocol-dependent distinction:

- OpenCode's Responses adapter retains an image in the output associated with its
  call identifier.
- Its Chat adapter moves tool-result images to a subsequent USER message. The
  native session retains the call/result association, but the image part on the
  wire has no explicit call identifier. Position/context is not the same guarantee
  as an explicit wire link.

These differences require the same fresh acknowledgement as other reviewed
transformations. A fake producer, fabricated native window or hosted-tool flag is
not used to conceal them. The destination model's actual image capability remains
a runtime prerequisite; synthetic provider tests do not prove a private gateway.

The initial PNG validator uses bounded decoded data, dimensions and decompression:
16 MiB decoded PNG bytes, 8192 per dimension, 16 million pixels and 64 MiB inflated
scanline data. It validates PNG structure/CRCs without rendering private images.
Unsupported ancillary/animation structures remain explicit refusals, not stripped
metadata. The source capture's own lower byte budget remains in force.

## Nested-memory carriers — DEV22

A closed `nested_memory` extension accepts captured project-memory attachments
with a proven compacted selection and at least one recorded rendering. Supported
fields are `type`, `path`, `displayPath` and `content`; the embedded content has
`path`, `type: "Project"`, textual `content`, and optional boolean
`contentDiffersFromDisk` and textual `rawContent`. The two paths must agree.
Unknown fields/types, conflicting paths and absent renderings are refused.

Recorded `rendered` and `renderedInHumanTurn` variants remain quoted USER DATA,
and the complete validated attachment is retained as a separate `captured_payload`.
Cached, raw and rendered contents may differ; all captured variants remain distinct.
The preview explicitly reports the payload representation and that these variants
are not reconciled. An absent raw or rendering variant is never reconstructed.
Recorded SYSTEM authority is declared rather than replayed.

Paths and display names are historical data. The migrator does not read those files,
write an AGENTS/CLAUDE memory file, or activate the captured guidance as destination
configuration. The private source archive and fresh reviewed acknowledgement remain
mandatory. This extension does not certify runtime equivalence or expand the strict
reader, client-version matrix or supported image formats. There is no new command,
API endpoint or dependency in DEV22; repository integration does not update installs.

For a non-compacted source with a genuinely open tool call, `--reviewed-portability`
is not the appropriate contract. Use the existing [single-node tree review](tree-migration.md#single-node-review-of-an-open-tool-call)
with separately acknowledged partial archival instead.

## Implementation and acceptance plan

- Reader: explicit mode through source capture, graph selection, quoted historical
  data and a deterministic content-free report. Strict defaults remain unchanged.
- Audit/projection: separate source selection and projection policy; independently
  compare conversation bodies and declared transformations. Bind source, prepared
  artifact, report and cold observation.
- Core: mode in options/fingerprints only when enabled, mandatory acknowledgement,
  durable archive and fail-closed publication. Preserve legacy hashes when disabled.
- Interfaces: CLI, tree, TUI and web show differences before confirmation; a mode
  toggle invalidates an old proposal and cannot silently grant acknowledgement.
- Validation: structural reproductions, adversarial mutations, default-mode
  regressions, native import/export and Chat/Responses continuation through a
  local scripted provider. No historical tool may execute during these tests.

The synthetic reproductions establish identifier relationships and selected
content preservation. Their fictional values and signatures do not certify the
original private requests, source cryptographic integrity or external model behavior.
Successful preparation alone is not a native import/continuation result.
