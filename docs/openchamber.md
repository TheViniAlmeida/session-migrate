# OpenChamber destinations

OpenChamber uses OpenCode 2.x to store and execute sessions. Its web port and its
OpenCode backend port can differ. SessionMigrate keeps `opencode` as the native
format and adds `openchamber` as an explicit destination client.

## CLI

```bash
smigrate transfer SOURCE_UUID --from codex --to openchamber \
  --target-cli /absolute/path/to/opencode \
  --target-server http://127.0.0.1:4096 --cwd /absolute/project/path
```

Supply the OpenCode server used by OpenChamber, rather than the OpenChamber UI
address. The backend must already be running. If it requires authentication,
provide its existing credential through `OPENCODE_SERVER_PASSWORD` in the
runtime environment. Credentials are never accepted in URLs or profile JSON.
SessionMigrate does not install, restart, or reconfigure either server.

The initial implementation supports local Linux backends owned by the current
user. The server origin must be HTTP on `127.0.0.1` or `[::1]`, with an explicit
port; `localhost` is normalized to `127.0.0.1` for both HTTP and CLI calls.
Redirects, different owners, ambiguous listeners and a
custom `OPENCODE_DB` outside the default selected database are rejected.

An existing wildcard-bound server may be reached through its literal loopback
origin, provided it is the sole proven listener; the migrator never changes
its binding or exposes another port.

Before sending authentication, the migrator checks the local listening socket,
its owning process and that process's open descriptor for the selected
`HOME`/`XDG_DATA_HOME` OpenCode database. It then compares `/api/info` version and
PID with the proven local process and requires the selected CLI to match the
server version. The socket, process epoch, database and options remain bound
through preparation and publication. This is a local process check, not a
cryptographic identity guarantee against a hostile process with the same UID.

Imports and exports use `opencode --server` rather than a new standalone server.
The native importer refuses an existing ID. The migrator checks that exact ID
before publication and verifies export and a paginated directory/title search
afterward. HTTP errors cannot become an empty catalog or a standalone fallback.
An uncertain post-import result requires inspection; do not automatically retry.

`smigrate convert ... --to openchamber --output bundle.json` only creates an
OpenCode 2.x artifact. It does not connect to or prove visibility in OpenChamber.

## TUI and web profiles

In `smigrate tui`, select **OpenChamber (OpenCode backend)** in the destination
step, then enter the native root, CLI and local backend origin. Existing
OpenCode destinations continue to use their original standalone behavior.

For the web dashboard, an OpenChamber destination is a trusted private profile:

```json
{
  "id": "openchamber-local",
  "name": "OpenChamber local",
  "format": "opencode",
  "root": "/home/YOU/.local/share/opencode",
  "cli": "/absolute/path/to/opencode",
  "environ": {"HOME": "/home/YOU"},
  "client": "openchamber",
  "server": "http://127.0.0.1:4096"
}
```

Add this row to the existing version-1 private profiles document, using the
normal opt-in profile workflow. Profiles stay owner-only with mode `0600`.
Older profile JSON and fingerprints remain unchanged. The web API selects an
opaque profile ID; it does not accept an arbitrary server URL from a request.

This release supports OpenChamber profiles as **destinations only**. Source
selection and continuity assessment/reconciliation through these client-marked
profiles are not yet supported. Ordinary OpenCode profiles retain their prior
behavior. Native in-place updates remain disabled; this integration does not
extend the handoff/return version proofs.

## Session exists but is absent from the UI

A successful result includes `target_format: "opencode"`,
`target_client: "openchamber"` and `backend_discovery_verified: true`. This proves
the session exists and can be found by its directory/title on the connected
backend; it is not a browser-rendering guarantee.

The frontend can still fail if its global session list returns an error, a
project filter excludes the directory, or it points at another backend. During
validation with OpenChamber 2.2.0/OpenCode 2.0.27, legacy session permission rules using
v1 keys (`permission`, `pattern`, `action`) caused large lists to return HTTP
500 when decoded against the v2 schema (`action`, `resource`, `effect`), independently of
the newly imported session. SessionMigrate does not silently repair other
sessions or treat such failures as missing data. Inspect the backend error
before copying the session again.

OpenCode 2.0.27 destination titles were separately verified through native
import, public discovery and an independent cold export. This title proof does
not enable new summary/return/update guarantees for that version.
