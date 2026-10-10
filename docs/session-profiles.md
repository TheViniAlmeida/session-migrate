# Session profiles (F2B core API)

`session_migrate.profiles` provides opt-in, content-free profiles for callers of
the shared core. This milestone adds no CLI commands or UI dependency.
Constructing a `Profile` does not create directories, launch a process, install
anything, or change `os.environ`. CWD remains an independent operation option.

A profile contains a filename-safe ID, display name, format, absolute native
root, optional absolute CLI path and HOME/XDG path overrides. Only `HOME`,
`XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME` and
`XDG_RUNTIME_DIR` may be persisted as overrides. Credentials, format-specific
configuration variables and arbitrary environment keys are rejected.
The native root is canonicalized; source roots must exist and be registered
with that exact format in the catalog. Roots are directories, except MastraCode
also accepts its canonical `mastra.db` file as an exact source/target root.
That file is passed directly to the native loader/installer; other filenames
are rejected rather than redirected to a sibling database. A target filesystem
root or canonical MastraCode database may be new.

`profile_from_catalog_root` requires an explicit format and environment mapping;
it does not guess a runtime environment from the catalog. OpenCode/Kilo require
an explicit executable CLI. Their source roots must pass the F2A native
store directory/database identity guard: the exporter opens exactly
`XDG_DATA_HOME/opencode` or `XDG_DATA_HOME/kilo`, including the selected store's
WAL/SHM directory. An explicit XDG data override that contradicts the root is
an error. New native targets must use the canonical `opencode`/`kilo` directory
name. Populated native targets also pass the existing directory/database guard.
No configuration or authentication files are copied between profiles.

## Small interface contracts

- `Profile.environment(base)` returns a fresh runtime mapping. A HOME override
  supplies HOME-local defaults for XDG data/config/cache/state unless explicitly
  overridden, and removes an inherited runtime directory. Source and target
  mappings are constructed independently from the same base; the source mapping
  is never used as the target base. Runtime credentials may remain in the caller's
  base environment but cannot enter the persisted override mapping.
- `SessionRef` identity includes format, canonical root and native ID, plus an
  optional profile ID. Listed refs also retain the catalog ID and a fingerprint
  of profile options. Reusing one with another root/profile, or after changing
  its CLI/environment, fails explicitly. Two roots may have the same native ID.
- `list_profile_sessions(catalog, profile, ...)` filters the exact root before
  applying pagination. It forwards query, status, kind, lifecycle and RFC-3339
  start-date filters. Each result carries profile ID, metadata and a qualified
  ref (or `None` when native identity is absent). Missing and unsupported rows
  remain visible by default, with their catalog reason; adapters determine
  which archived/subagent schemas are transferable.
- `resolve_source` verifies the catalog identity/status and physical path under
  the selected root. `read_session` then loads the authoritative file or native
  export and verifies its format/native ID. Catalog metadata is derived and may
  be stale; a refresh is explicit and does not substitute for authoritative read.
- `run_profile_operation` binds a transfer request to source ref and target
  profile, rejects conflicting free selectors and calls the existing adapters.
  `run_operation(..., source_environ=...)` resolves source HOME, CLI, path,
  source CWD, shared database defaults and catalog in the source environment;
  target paths, CWD and installers continue to use `environ`. Omitting the new
  keyword preserves the legacy single-environment contract.

Ordering follows the catalog: physical files use filesystem mtime; OpenCode/Kilo
use native `time_updated` with database mtime fallback; other shared databases
use the native inventory update timestamp with database mtime fallback.
`ordering_source` declares that policy. `started_at_source` is `native_metadata`;
a missing timestamp stays missing. Neither value claims observed user activity.
Bodies are not indexed, persisted in profile state, or included in progress.
Read/parsing/export may block and should run in a UI worker in the next milestone.

## Optional private storage

`ProfileStore(absolute_path).create(profiles)` explicitly creates one JSON
snapshot with `schema_version: 1`. IDs and case-insensitive names must be unique.
`load()` and `get(profile_id)` reject absent IDs, unknown schemas, duplicate JSON
keys, unknown fields, invalid formats/environment and files over 1 MiB.

Persistence requires POSIX ownership and no-follow support. Directory traversal
uses directory descriptors and rejects symlinks. The storage directory must be
owned by the current user with mode 0700; the file must be an owned, single-link
regular file with mode 0600. Newly created directories/files use these private
permissions and the complete write is flushed/fsynced. Creation is exclusive:
an existing snapshot, including a symlink, is never overwritten. Editing,
replacing or removing stored profiles needs an explicit future API contract.
No default storage path or automatic writes are introduced.

## Capabilities and validation boundary

`capabilities_for(format, schema=...)` and `list_capabilities()` describe all
18 source/target adapters. Symbolic schema names identify the implemented
contracts; they do not bypass native version/shape validation. Codex exposes
legacy and paginated sources, with only legacy output. OpenCode exposes legacy
and v2 transfer schemas. Pi/OMP expose v3; Hermes/Devin expose pinned database
and portable export schemas; other formats expose their existing native adapter.
Portable image support is scoped for Claude/Codex/OpenCode, Cursor declares
text-only, and unassessed media capabilities remain unknown.

Native title override and in-place update are false for every schema, with
`proof_version=None` and an explanation of the required F3/F5B proof. Discovering
existing native titles, or importing their current default title, does not enable
those future operations. Unknown formats/schemas fail explicitly. Native parsers
and installers remain authoritative for schema validation and loss accounting.

Synthetic tests cover same IDs across roots, distinct titles, pagination,
status/date filters, absent sources, archived Codex, unsupported Claude
sidechains, changed identity/environment, private storage and source/target
runtime isolation. The optional stock OpenCode 2.0.22/2.0.23 oracle imports the
same ID into two temporary native stores and re-exports each selected title.
It selects `SESSION_MIGRATE_TEST_OPENCODE_V2` first, with PATH discovery as a
local fallback. No real session store or provider is required.
