# /etc/vula/install.env — install-time constants

Written once by `install.sh` during `phase_print_app`. Read by every
script in `system/bin/` that needs to know who the operator user is
or where the source tree lives.

## Format

Plain `KEY=VALUE`, one per line, sourced by shell. Same format as
the repo's `.env` and `/etc/vula/creds.env`.

## Keys

| Key           | Example                        | Used by                              |
|---------------|--------------------------------|--------------------------------------|
| `REAL_USER`   | `pos`                          | vula-clamscan.sh, vula-print-update.sh |
| `REAL_HOME`   | `/home/pos`                    | (informational)                      |
| `SOURCE_DIR`  | `/home/pos/Vula-Print`         | vula-print-update.sh, vula-update.sh |
| `VULA_VERSION`| `1.1.871`                      | (informational, bumped by installer) |

## Why not pass as systemd args?

Because systemd `ExecStart` lines would then need per-install
substitution, which means the installer writes the unit file rather
than copying it. Keeping units static lets `diff` verify they match
the repo, and keeps the source of truth in this tree.
