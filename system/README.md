# system/ — files installed to system locations

Everything under `system/` is the **source of truth** for files that
`install.sh` places on the target machine. Nothing here is executed
from this location — it is copied to the destination by the installer.

## Layout

| Repo path              | Installed to                    | Mode |
|------------------------|---------------------------------|------|
| `system/bin/*.sh`      | `/usr/local/bin/`               | 0755 |
| `system/systemd/*`     | `/etc/systemd/system/`          | 0644 |
| `system/polkit/*.rules`| `/etc/polkit-1/rules.d/`        | 0644 |
| `system/udev/*.rules`  | `/etc/udev/rules.d/`            | 0644 |

## Rules

1. **No dynamic content.** Every file here is static. If a script needs
   values that change per install (username, source dir), it reads them
   from `/etc/vula/install.env`, which the installer writes.

2. **No embedded heredocs in install.sh.** The installer copies from
   this tree; it does not generate system files. Adding a new system
   file means adding it here, not editing install.sh.

3. **uninstall.sh removes what's here.** The uninstaller enumerates
   `system/bin/`, `system/systemd/`, etc. to know what to remove. If
   you add a file here, uninstall picks it up automatically.

4. **Verify with diff.** After an install on any device:
   `diff -r system/systemd/ /etc/systemd/system/` (filtered by name)
   should show no differences for the vula-* units.
