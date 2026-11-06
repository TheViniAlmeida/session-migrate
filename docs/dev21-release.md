# DEV19–21 release reference

Updated 6 November 2026 (UTC−03). These are fork prereleases, not PyPI releases.

## Delivered migration behavior

- DEV19: opt-in reviewed portability from proven Claude 2.1.309 compacted captures
  to exact OpenCode 2.2.5, with declared runtime/ordering differences, separate
  acknowledgement and a private archive of the complete selected capture.
- DEV20: bounded inline PNG in completed, non-error tool results, retaining bytes
  and native tool association. Chat moves image parts into USER without a wire
  call identifier; Responses retains the output association. Preview discloses this.
- DEV21: catalog guards distinguish stable system-ancestor identity from unrelated
  directory timestamp changes. A narrowly verified unprivileged process may ignore
  only those system-ancestor ctime changes; private directories and the database
  retain strict checks. Missing privilege evidence falls back to strict checks.

The [compatibility matrix](current-client-compatibility.md) retains Codex 0.166.0
and Claude Code 2.1.317/318 contracts. No additional client version is certified
by this catalog correction. Reviewed coverage remains PARTIAL/UNKNOWN; it does
not restore source runtime state or authorize an in-place session update.

## Commands and API

```bash
smigrate tui
smigrate proposal transfer SOURCE_ID --from claude --to opencode \
  --target-cli-version 2.2.5 --reviewed-portability
smigrate transfer SOURCE_ID --from claude --to opencode \
  --target-cli-version 2.2.5 --reviewed-portability
```

The transfer displays its proposal and asks for confirmation. Deliberate
noninteractive acceptance uses `--acknowledge-reviewed-portability`; selecting
`--reviewed-portability` alone is not consent. Proposals and dry runs do not publish.

Existing web preparation/confirmation routes use boolean `reviewed_portability`
and `acknowledge_reviewed_portability`. No new endpoint is introduced in DEV20/21.
Trees retain their separate partial acknowledgement and publication-mode contract.
See [reviewed portability](reviewed-portability.md), [tree migration](tree-migration.md)
and [dashboard API](web-dashboard.md) for full request and resume rules.

## Validation and deployment boundaries

The authorized migration flow reported 16 functional independent conversations:
15 new imports and one preserved existing conversation. New imports passed cold
verification and two real continuation turns. A separate fictitious image canary
recognized nine colors without tools; the historical PNG independently matched
its original bytes and native tool link. These checks do not establish universal
model vision support, source runtime equivalence or peer messaging between roots.
Development/review did not open private transcripts or render private images.

At this delivery, user aliases and the shared installation remain DEV19; DEV20 is
available through its approved private versioned prefix. Repository integration is
separate from installation. DEV21 is developed and reviewed in an isolated worktree;
no delivered functional conversation or existing versioned prefix is modified by
its tests. Future installation must verify the built package separately.

Initial DEV20 CI web failures were retained; a controlled rerun on the same commit
passed all 39 checks. Separate synthetic experiments reproduced an ancestor ctime
failure under unrelated `/tmp` churn. This is a proven mechanism, not a claim that
it explains the original CI failures. DEV21 adds focused regression coverage for
that mechanism while preserving private-directory and database guards.
