# Proton Drive integration

DocNest stores documents in Proton Drive through the official Proton Drive CLI (`proton-drive`, tested with **v0.8.0**, SDK js@0.21.0). This document records how the CLI behaves and how DocNest runs it safely in a container.

## How the CLI stores credentials

After `auth login`, the CLI keeps one session object:

```json
{ "cachePassword": "…", "userKeyPassword": "…",
  "session": { "uid": "…", "accessToken": "…", "refreshToken": "…" },
  "telemetryEnabled": true }
```

`userKeyPassword` unlocks the account's private keys, so this object is as sensitive as the account password. The CLI rewrites it whenever tokens are refreshed, so the store must be **writable**.

The store is chosen with `PROTON_DRIVE_CREDENTIALS_STORE`:

| Value | Where | Usable in Docker? |
|---|---|---|
| `keychain` (default) | OS keyring via `Bun.secrets` (libsecret, service `ch.proton.drive/drive-sdk-cli`, name `auth-session`) | No (needs D-Bus + an unlocked keyring) |
| `unsafe_file` | `<app dir>/auth-session.json`, mode 0600, **plaintext** | Works, but stores plaintext |
| `pass` | Runs `pass show/insert -f -m/rm -f ch.proton.drive/drive-sdk-cli/auth-session` | **Used by DocNest**, via its own `pass` shim |

Other environment variables the CLI reads:

| Variable | Effect |
|---|---|
| `PROTON_DRIVE_CACHE_DIR` | One directory for the cache, app data and log (otherwise `XDG_CACHE_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME`) |
| `PROTON_DRIVE_LOG_LEVEL` | `DEBUG` (default), `INFO`, `WARNING`, `ERROR`. At DEBUG level the log contains the account email address and node IDs. |
| `PROTON_DRIVE_UNSAFE_CACHE` | Turns off cache encryption. **Never set this.** By default the cache is encrypted with `cachePassword`. |
| `PROTON_DRIVE_BASE_URL` | API host (default `drive-api.proton.me`) |

Telemetry follows the Proton account setting. To turn it off, disable usage statistics in your Proton account settings.

## DocNest's approach

- **`pass` shim** (`docker/proton/docnest-pass`, installed as `/usr/local/bin/pass`): implements exactly the three commands the CLI uses.
  - Stores the session with AES-256-GCM in `$DOCNEST_PROTON_SESSION_DIR/auth-session.enc` (mode 0600, directory 0700). The entry name is used as associated data, which binds the ciphertext to that entry.
  - The key is derived with HKDF-SHA256 (`info=docnest/proton-session/v1`) from the DocNest master key (Docker secret). The plaintext session never touches the disk. A copy of the volume without the master key is useless.
  - Writes are atomic (temp file, fsync, rename, directory fsync) under an flock. Only the one entry name is accepted.
- **`docnest-proton` wrapper** (`docker/proton/docnest-proton`): every CLI call except `auth` goes through `flock`. The CLI rotates refresh tokens, and two processes refreshing at the same moment could invalidate the session. Parallel calls work as such, but DocNest serializes them on purpose.
- **Container**: non-root UID 10001, read-only root filesystem, all capabilities dropped, `no-new-privileges`. CLI state lives in the volume `/var/lib/docnest/proton` (`session/` and `cache/`). Log level is `WARNING`.
- **Binary**: downloaded during the image build from `https://proton.me/download/drive/cli/<version>/<platform>/proton-drive` and pinned by SHA-256. It is never committed to Git. amd64 images contain both `linux-x64` and `linux-x64-baseline`: the standard build needs AVX2, which many VMs don't expose (generic CPU models such as Proxmox's `kvm64`), and crashes there with exit code 132. `docnest-proton` checks `/proc/cpuinfo` on every call and uses the baseline build when AVX2 is missing. arm64 builds use `linux-arm64`.

## One-time login (operator)

```bash
docker compose exec app docnest proton-login
```

The CLI prints a sign-in URL. Open it in a browser on **any** device (it does not have to be the server), sign in to Proton, and keep the terminal open until it reports *Authentication successful*. Nothing has to be installed on the host. Check the result with:

```bash
docker compose exec app docnest proton fs list /my-files
```

The session is stored encrypted in the `proton` volume and survives restarts and upgrades. If it ever expires, DocNest shows a banner. Uploads are still accepted and kept encrypted in the intake until you run `proton-login` again.

Interactive `auth` commands bypass the CLI lock, so a pending login never blocks document storage. All other calls wait for the lock for at most `DOCNEST_PROTON_LOCK_WAIT` seconds (default 900).

## Verified CLI behaviour (2026-10-04, v0.8.0)

| Operation | Command | Result |
|---|---|---|
| Create folder | `fs create-folder /my-files Name --json` | Node JSON (`uid`, …), exit 0 |
| Upload | `fs upload -f <strategy> -t <file> <parentPath> --json` | `{"transferredItems":1,"transferredBytes":…,"skippedItems":0,"failedItems":0,"failures":[]}` |
| Re-upload of identical content | same command | `skippedItems: 1` (content dedupe) |
| Info | `fs info <path> --json` | Includes `uid`, `activeRevision.claimedSize`, `activeRevision.claimedDigests.sha1` |
| Download | `fs download -f remove <path> <localDir> --json` | Byte-identical (SHA-1 verified) |
| Missing node | `info` or `download` | stderr `Node not found: <name>`, **exit 1** |
| Not logged in | any command | `You need to login first`, **exit 1** |
| Trash and delete | `fs trash <path>`, then `fs delete /trash/<name>` | Exit 0. `delete` only works on trashed items. |
| Concurrency | 2 parallel `fs list` | Both succeed |
| Latency | single `fs info` | ~3.4 s (session load, key decryption, API call) |

Implications for the storage adapter:
- Always pass `--json`, and treat a non-zero exit code or `failedItems > 0` as an error.
- Always pass an explicit conflict strategy (`-f`/`-d`). Otherwise the CLI asks interactively.
- Verify uploads with `fs info` (`claimedSize` + SHA-1 against the local file) before deleting the intake copy.
- Upload with `-t` (skip thumbnails).
- Expect ~3–4 s per call. This is fine for background jobs, but the latency is noticeable when opening a document. An optional encrypted view cache helps there.
- `fs list` can raise `EPIPE` when its output is cut off early. The adapter must read stdout completely.

## License

The CLI is open source under the MIT license (source in the [Drive SDK repository](https://github.com/ProtonDriveApps/sdk), `cli/`, © Proton AG). MIT permits redistribution, so the pinned binary is bundled in the public DocNest image together with its license text (`/opt/proton-drive/LICENSE.md`).

## Open points

- Session lifetime without use. The worker's health check runs `fs list /` every 10 minutes, which also keeps the session refreshed.

## Adapter verification (2026-10-04)

`ProtonDriveCliBackend` was tested against a real account: nested folder creation, upload with size + SHA-1 verification, idempotent re-upload, byte-identical download, folder trash + permanent delete (test folders removed afterwards). Inside the read-only production container the CLI runs with the encrypted `pass` shim and reports `needs_reauth` correctly when not logged in.
