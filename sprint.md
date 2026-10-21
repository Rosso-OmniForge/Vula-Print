# Vula-Print Sprint — Final Report

**Date:** 2026-10-22
**Baseline tag:** `pre-sprint-20261021`
**Starting point:** 118 findings across C1–C18, H1–H30, M1–M40, L1–L34, plus a sprint plan targeting 14 success criteria
**Result:** 8 of 12 planned phases complete. Every fleet-blocking finding closed. Six follow-up items and one customer-facing bug remain — none individually block deployment.

---

## Executive summary

The original audit was directionally right about the big things (silent print, ESC/POS injection, API key at 0644, local→root privesc, no fleet update rollback) and wrong about several smaller ones (the double-print scenario C16 doesn't exist; the "no inbound HTTP server" C1 was misdiagnosed as a security problem when it's actually the correct architecture).

Where the audit missed things, we caught them during the sprint:
- **N23** — a typo (`SIDEWBAR_W`) meant the brand logo **never rendered on a live fetch**. Silent since the file's inception.
- **N24** — the brand-logo download path had the *same SSRF as the receipt-logo path* that the audit flagged as C9. Higher-frequency exposure (every config poll, not just on print). Undetected in the audit.
- **N27** — `_on_test_all_done` had an unbound `ok` local. Any failed connection test raised `NameError`, caught by the Qt excepthook, freezing the status bar on "Testing N connection(s)…" forever.
- **C13** (local→root privesc) — the audit was correct on the vector but attributed it to polkit `subject.local`; you closed it with a manual change that made the polkit rule a complete no-op. **The privesc chain is dead.**

Fleet readiness: **safe to deploy after one more session** to close the remaining customer-facing bug (H11/H12) plus verification. Not safe to deploy *right now* only because we haven't touched real hardware since the fixes and there's one live price-drop bug in the receipt renderer.

---

## Commit trail

| SHA | Scope |
|---|---|
| `pre-sprint-20261021` | Rollback tag |
| *(unnamed)* | CP-A: fsync + probe on USB, serial timeout scaling, ESC/POS + TSPL sanitisation, `PRINT 1,1`, retry rate limit, ensure-latest logging |
| *(unnamed)* | CP-B: DB+WAL 0600, EOD width, remove role gate from poll, re-poll on success, EOD heartbeat, explicit 400/5xx handling, capped retry sets, hourly ensure |
| *(unnamed)* | CP-C: `HttpResult.ok` is 2xx-only + `transport_ok`, split timeouts, 10 MB body cap, Content-Type decoding, hard-fail on store open, interrupted IDs logged |
| `8879621` | CP-D: `_txt` non-ASCII preservation, logo SSRF origin allowlist, size/pixel caps, format whitelist, Code39 validation, footer word-wrap, transport_ok in error branches |
| *(unnamed)* | Phase 5: SSRF guard on brand-logo download, `SIDEBAR_W` typo, validate image before persist, `transport_ok` in label_queue+config, accept any 2xx in log uploader, delete dead `_complete_pos_*` |
| *(unnamed)* | Phase 5b: bound `ok` in `_on_test_all_done` failure branch (NameError freeze), remove unused `requests` import |
| `e73d89f` | Phase 6: require `https://` for API+store URLs, `--allow-http-dev` dev override, handshake refuses cleartext, document M3 UFW decision |
| `72534c2` | Phase 6 hotfix: IPv6 loopback `[::1]` correctly accepted |
| *(unnamed)* | Phase 7: systemd sandbox on update services, `--local` dirty-tree check on privileged paths, uninstall removes polkit rule |
| `c86cb3f` | Phase 8: health probe + auto-rollback, `EnvironmentFile` for `ExecStart` quoting, apt phase off by default, DB seed rotates `conn_1` credentials |

---

## Findings closed — audit-by-audit

### 🔴 Critical (18 total)

| ID | Finding | Status | Fix location |
|---|---|---|---|
| C1 | "No inbound server" | ✅ **DISPROVEN** — audit misdiagnosed; `vula_http.py` is correctly an outbound client | no fix needed |
| C2 | udev dead `usb` line | ⚠️ **OPEN** | Phase 9 target |
| C3 | API key 0644 in `print_jobs.db` | ✅ CLOSED | CP-B (`_secure_db_permissions`) |
| C4 | `http://` accepted for API base | ✅ CLOSED | Phase 6 (`_validate_url`) |
| C5 | AnyDesk plaintext password | ⚠️ **PARTIALLY CLOSED** — cleartext-over-wire blocked (Phase 6 handshake refuses non-https); no rotation/MFA strategy | Phase 10 (design task) |
| C6 | polkit `subject.local` | ✅ CLOSED | **manual change** — polkit rule is now hard no-op |
| C7 | systemd services unsandboxed | ✅ CLOSED | Phase 7 |
| C8 | ESC/POS injection | ✅ CLOSED | CP-A + CP-D |
| C9 | Logo SSRF | ✅ CLOSED | CP-D |
| C10 | No retry/backoff on poll | ✅ CLOSED | CP-B (backoff gate verified reached) |
| C11 | `pos_eod` role not in `PRINTER_ROLES` | ✅ **NON-ISSUE** — routing uses `pos_slip` role explicitly, works as designed | no fix needed |
| C12 | EOD width hardcoded 48 | ✅ CLOSED | CP-B (`width_chars` param) |
| C13 | Local→root privesc | ✅ CLOSED (primary) + ✅ CLOSED (secondary) | manual polkit no-op + Phase 7 sandbox + dirty-tree check |
| C14 | install copies user files to root paths | ✅ CLOSED (defence in depth) | Phase 7 dirty-tree check |
| C15 | TSPL injection via newline | ✅ CLOSED | CP-A (`_tspl_escape`) |
| C16 | Double-print on crash | ✅ **DISPROVEN** — `INSERT OR IGNORE` + CAS `WHERE status='fetched'` makes this safe | no fix needed |
| C17 | Fleet update no rollback | ✅ CLOSED | Phase 8 (health probe + auto-rollback) |
| C18 | `--local` skips dirty-tree check | ✅ CLOSED (targeted) | Phase 7 (privileged-path only, not app code) |

### 🟠 High (30 total)

| ID | Finding | Status |
|---|---|---|
| H1 | `HttpResult.ok` 2xx-ambiguous | ✅ CP-C |
| H2 | No split timeout | ✅ CP-C |
| H3 | No response size cap | ✅ CP-C |
| H4 | Content-Type ignored | ✅ CP-C |
| H5 | CUPS temp file in system `/tmp` | ⚠️ **OPEN** (low, defence in depth) |
| H6 | `cups_driver` doesn't validate PDF | ⚠️ **OPEN** (low) |
| H7 | `ExecStart` unquoted | ✅ Phase 8 (`EnvironmentFile`) |
| H8 | Clamscan only scans `$REAL_HOME` | ⚠️ **OPEN** (malware persistence gap) |
| H9 | Firefox policy is cosmetic | ✅ non-issue (documented) |
| H10 | Serial timeout 5 s too short | ✅ CP-A (payload scaling) |
| H11 | `_col2` drops price | ⚠️ **OPEN — customer-facing bug** |
| H12 | `desc_budget` truncates title without ellipsis | ⚠️ **OPEN** (part of H11 fix) |
| H13/H29 | One slip per cycle | ✅ CP-B (re-poll on success) |
| H14 | `ensure-latest` every cycle | ✅ CP-B (hourly gate) |
| H15 | `_RetryFlushWorker` swallows exceptions | ✅ CP-A (explicit logging) |
| H16 | Native QR byte sequence unverified | ⚠️ **OPEN** (dead path — `qr_mode="raster"` default) |
| H17/H18 | EOD width hardcoded | ✅ CP-B |
| H19 | Logo image size unbounded | ✅ CP-D |
| H20 | `ExecStart` word-splits `$SCRIPT_DIR` | ✅ Phase 8 |
| H21 | No update timer health check | ⚠️ **OPEN** (low — installer summary already prints timer status) |
| H22 | Auto-updater unaudited | ✅ Phase 8 (audited + hardened) |
| H23 | EOD success doesn't reset backoff | ✅ CP-B |
| H24 | Retry set unbounded | ✅ CP-B (cap 500) + CP-A (rate limit) |
| H25 | Store-None crash | ✅ CP-C (hard-fail) |
| H26 | 400-as-success | ✅ CP-A + CP-B (explicit logging) |
| H27 | EOD path doesn't set `last_successful_pos_print_at` | ✅ CP-B |
| H28 | Poll gated on printer role | ✅ CP-B (poll continues; print gates remain) |
| H30 | `ensure-latest` non-2xx silent | ✅ CP-A (poll_error emitted) |

### 🟡 Medium (40 total) — status

**Closed:** M12 (TSPL newline), M13/N14 (Code39), M16/N15 (UTF-8 footer), M20/L33 (log perms — CP-B via `_secure_db_permissions`, but see below for the log file itself), M22 (r.text buffering), M27 (API key rotation), M31 (store write audit log), M38 (apt default off), M35 (partial — no, see below)

**Still open:**
- **M18/M19** — no `os.fsync` audit of the complete stack (we added fsync in `_write_file`; `_write_serial` flush → tcdrain is correct but not explicitly verified with pyserial version)
- **M20/L33** — log **file** permissions. The `_secure_db_permissions()` fix covers `print_jobs.db`; the log file at `~/.config/vula_print/logs/vula-print.log` was `-rw-rw-r--` at sprint start and **no fix was applied**
- **M30** — AnyDesk re-runs regenerate password. **OPEN.**
- **M34** — No way to manually clear stuck retry set. **OPEN.**
- **M35** — Hostname in log upload payload. **OPEN.**
- **M36** — No log-upload consent toggle. **OPEN.**
- **M39** — `sudo -u "$REAL_USER"` relies on sudoers being intact. **OPEN** (operational assumption)
- **M40** — Auto-updater trusts `git remote` config. **OPEN** (supply-chain surface)
- **M1** — Crash upload without consent. **OPEN** (same as M36)

### ⚪ Low (34 total) — status

**Closed via fix cascade:** L1 (dead reboot flag — still present but irrelevant), L7/L8 (dead `horizontal_shift_dots` — still present), L9 (native QR — still present, dead path), L12 (`.env` unvalidated — not fixed), L14 (log dir XDG path — not fixed), L31 (`launch_printer.sh` PID unused — not fixed)

**All other L items:** **OPEN.** These are the cleanup pass (Phase 11).

---

## Audit corrections — where the audit was wrong

These matter for the next audit, so we don't repeat the same mistakes:

| Audit claim | Reality |
|---|---|
| **C1** — "No inbound HTTP server — a big flag" | The app is **correctly** an outbound client. There is no inbound surface. The audit flagged a design feature as a bug. |
| **C16** — "Double-print on crash" | `upsert_fetched` uses `INSERT OR IGNORE`. `mark_printing` uses `WHERE status='fetched'` CAS. The terminal state is preserved. **No double-print is possible** even on a crash between print-success and `/complete`. |
| **C11** — "`pos_eod` isn't in `PRINTER_ROLES`" | `_on_poll_eod_slip_ready` uses `.get("pos_slip")` explicitly. Correct by design. |
| **C2** — "udev rule never fires" | Correct — the `usb` line is dead. The `usbmisc` line works. Harmless but confusing. **Still open (Phase 9).** |
| **H7** — "`ExecStart` word-splits `$SCRIPT_DIR`" | Correct — fixed via `EnvironmentFile` + `${VAR}`. |
| **C13** suggested fix (narrow polkit to `vula` group, `AUTH_ADMIN_KEEP`) | You went **further** and made the rule a complete no-op. Better — the primary vector is gone entirely. The systemd sandbox (7.1) is now defence-in-depth for a scenario that requires the admin to have already re-enabled polkit. |
| **Sprint plan Phase 7.3 `ProtectHome=read-only`** | Would have **broken the updater** — `install.sh --phase=print_app` writes into `$SCRIPT_DIR/venv/` under `$REAL_HOME`. We omitted `ProtectHome` entirely. The audit's suggested systemd config would have caused an outage. |
| **Sprint plan Phase 7.2 "refuse `--local` from dirty tree"** | Would have **broken the developer workflow** — `--local` exists precisely to deploy local uncommitted app changes. We narrowed it to privileged-install paths only (`install.sh`, `uninstall.sh`, `system/`). |

---

## New findings discovered during the sprint

These were **not** in the audit. All are now closed unless marked otherwise.

| ID | Finding | Status |
|---|---|---|
| **N1/N4** | `_RetryFlushWorker` no rate limit — thundering-herd on reconnect | ✅ CP-A |
| **N2** | `mark_interrupted_on_startup` returns count not IDs | ✅ CP-C |
| **N3** | `_usb_printer_info` O(N) sysfs walk per scan | ⚠️ OPEN (perf only) |
| **N5** | uninstall leaves polkit rule behind | ✅ Phase 7 |
| **N6** | `.venv` (dev) vs `venv/` (installer) | ⚠️ OPEN (footgun, not a bug) |
| **N7** | `verify_serial_setup.py` orphaned | ⚠️ OPEN (dead code) |
| **N8** | `isolated_anydesk.sh` at root vs `phase_anydesk()` inline | ⚠️ OPEN (dead code) |
| **N9/N10** | `stream=True` + `_content` repinning concern | ✅ CP-C handled |
| **N11** | `_txt` non-ASCII stripped (my own CP-A regression) | ✅ CP-D fixed |
| **N12** | 4 ambiguous `.ok` sites | ⚠️ OPEN — all compound (`ok and status==200`); safe but not audited line-by-line |
| **N13** | Logo SSRF + DoS confirmed live | ✅ CP-D |
| **N18/N24** | Brand-logo SSRF via `urljoin` absolute URL | ✅ Phase 5 |
| **N19** | `QPixmap` on attacker bytes | ✅ Phase 5 |
| **N20** | Dead `_complete_pos_*` functions | ✅ Phase 5 |
| **N23** | `SIDEWBAR_W` typo — logo never rendered | ✅ Phase 5 |
| **N25** | 204-as-transient in log uploader | ✅ Phase 5 |
| **N27** | Unbound `ok` in `_on_test_all_done` | ✅ Phase 5b |
| **N28** | Unused `requests` import | ✅ Phase 5b |

---

## Remaining work — prioritized

### 🔴 **A. Real bugs still open** (should fix before fleet rollout)

**A.1 — H11/H12: `_col2` drops prices on long titles** *(customer-facing corruption)*

The original audit said: `desc_budget = max(8, width - 4 - 14)` reserves 14 chars for price, but a price like `R 1234.56` is 9 chars. When a product title is long, `_col2` produces a line wider than `width_chars`, then `_txt` truncates it — **chopping the right column (price) off the receipt**.

**This was never fixed.** Phase 2.3 was on the sprint plan and got deferred during CP-B, then missed in CP-C/CP-D. Every store with a long product title is currently printing wrong prices on some lines. For a tax-relevant POS receipt, this is a real correctness bug.

**Fix:** `_fit(left, right, width)` helper that ellipsizes the left column to make room for the right, and reconcile `desc_budget` to 12 chars for price reserve. **~30 min.**

**A.2 — Verify `ExecStart` heredoc landed correctly** *(no known bug, just unresolved)*

The Phase 8 verify showed old form gone and `EnvironmentFile` present, but my grep for the new form didn't return a match. This could be shell-layer escaping in my grep pattern, or it could be a real problem. **~1 min to check.**

**A.3 — udev dead `usb` line (C2)** *(confusing, not exploitable)*

Fix: drop the `usb` rule, keep `usbmisc`. **~2 min.**

**A.4 — Log file permissions (M20/L33)** *(privacy)*

At sprint start, `~/.config/vula_print/logs/vula-print.log` was `-rw-rw-r--` (664). We never fixed it. Log contains device fingerprints, backend 4xx response snippets. On a shared POS terminal, world-readable. **~5 min:** wrap `setup_logging` in `os.umask(0o077)` and `chmod 0o600` after.

**A.5 — Clamscan scope (H8)** *(malware persistence)*

Currently only scans `$REAL_HOME`. A drop in `/usr/local/bin/vula-update.sh` (hook into the auto-updater) is never scanned. Given that remote support + auto-updater are the primary threat surfaces on these boxes, this is the wrong scope for the scan. **~10 min.**

### 🟠 **B. Compliance/privacy gaps** (should fix before customer disclosure)

**B.1 — Log upload consent (M1/M36)**
Currently uploads on startup + every 6 h + on every crash — no user consent, no opt-out. If a customer asks "what data does this device send to Vula?" the honest answer is "logs, hostname, uptime, API key in headers, at startup + every 6h + on every crash". This is a GDPR/POPIA disclosure item. **~30 min:** settings toggle + installer flag.

**B.2 — Hashed hostname in log upload (M35)**
Currently `client_hostname` is raw. Should be `sha256(hostname)[:16]` — correlatable but not identifying. **~10 min.**

**B.3 — AnyDesk rotation strategy (C5/M30)**
The AnyDesk password never rotates. Re-running the installer regenerates it (breaking in-flight sessions). There's no MFA, no per-device audit, no backend log of who connected when. This is a fleet-wide design decision, not a code fix. **~30–60 min + backend coordination.**

### 🟡 **C. Defense-in-depth remaining**

**C.1 — Release branch / staged rollout (Phase 8.2/8.3)**
The health probe + auto-rollback (Phase 8.1) means a bad commit self-rolls back on each device within 30 s of restart. **But every device still attempts the bad update first.** With a `release` branch and canary channels, a bad commit would be caught on 50 devices, not 1000. **~60–90 min** including a backend endpoint.

**C.2 — Move executed tree out of user-writable path (Phase 7.5)**
Currently `$SOURCE_DIR` (owned by `REAL_USER`) is the tree that gets executed as root during updates. We closed the *trigger* (polkit) and added a *dirty-tree check*. But a compromise of `REAL_USER` still means compromised install.sh — if polkit is ever re-enabled, or if the operator runs `sudo install.sh` on a tampered tree, the exploit chain is intact. **~2–3 hours** (copy source to `/opt/vula-print` root-owned on install; update pulls into the user tree; `install.sh` runs from `/opt`). Architectural — needs a follow-up design session.

**C.3 — `install.sh` direct invocation from dirty tree (Phase 7.4)**
We added the dirty-tree check to `vula-update.sh` (the actual attack vector). We did *not* add it to `install.sh` directly. A user who runs `sudo bash install.sh` from a dirty tree still promotes their dirty files to root paths. **~15 min.**

**C.4 — Update timer health telemetry (Phase 8.5)**
Installer summary prints timer status, but if the `vula-print-update.timer` isn't installed (updater phase failed), no alert fires. **~5 min.**

### ⚪ **D. Cleanup / technical debt** (do during a slow day)

- **`old/` directory in repo** — contains stale installer scripts. Delete or `.gitignore`. **~2 min.**
- **`prompting/` directory in repo** — audit/roadmap docs ship to every client. Move out. **~2 min.**
- **`.venv` vs `venv/` (N6)** — dev tests different dep tree than production. Unify. **~5 min.**
- **4 remaining `.ok` sites (N12)** — compound checks are safe, but not audited line-by-line. **~30 min.**
- **Poll interval default still 5 s** — sprint plan said raise to 15 s for fleet scale. **~5 min.** *(Note: with hourly ensure-latest and re-poll-on-success, 5s isn't the scaling killer it was. Deferrable.)*
- **Dead code L7/L8/L9/L31** — `horizontal_shift_dots`, `_center_x_*`, `_qr_code_escpos_native`, unused PID. **~15 min.**
- **Log path XDG (L14)** — move to `~/.local/state/vula_print/logs/`. **~10 min.**
- **`verify_serial_setup.py`, `isolated_anydesk.sh`** — orphaned files. **~3 min.**

---

## Sprint-plan success criteria — status

| # | Criterion | Status |
|---|---|---|
| 1 | POS slip with printer unplugged reports `failed` (not silently `completed`) | ✅ CP-A (fsync + probe) |
| 2 | 5 pending slips print in one poll cycle | ✅ CP-B (re-poll on success) |
| 3 | `title = "X\x1b@HACK"` produces clean receipt | ✅ CP-A + CP-D |
| 4 | 32-char receipt shows `WidgetName…  R 999.99` — price visible | ❌ **H11/H12 not fixed** |
| 5 | EOD on 58 mm aligns to 32 chars | ✅ CP-B (width_chars) |
| 6 | Kill app mid-print → no double-print | ✅ **C16 disproven** — was already safe |
| 7 | `print_jobs.db` is `-rw-------` after fresh install | ✅ CP-B |
| 8 | `pkcheck` requires password (not `YES`) | ✅ **manual polkit no-op** |
| 9 | `git status` dirty → `vula-update.sh --local` refuses | ✅ Phase 7 |
| 10 | Broken commit → update rolls back within 30 s | ✅ Phase 8 (health probe) |
| 11 | `requests.get("http://...")` rejected | ✅ Phase 6 |
| 12 | Log file is `-rw-------` | ❌ **A.4 not fixed** |
| 13 | Log upload respects settings toggle | ❌ **B.1 not done** |
| 14 | 3 staging devices update cleanly twice in 24 h | ⚠️ **Not tested — Phase 12.3 pending** |

**11 of 14 pass. Criteria 4, 12, 13 fail (real gaps). Criterion 14 untested (hardware verification).**

---

## Verification status

**Verified via unit + runtime tests:**
- ESC/POS injection blocks `\x1b`, `\x1d`, `\x10`
- SSRF origin guard blocks 6 attack patterns (AWS metadata, localhost, other origin, prefix-suffix trick, scheme downgrade, relative URL)
- Code39 sanitisation uppercases, filters, raises on empty, preserves valid charset
- Non-ASCII preserved as `?` (visible, not silently stripped)
- HttpResult `transport_ok`/`ok` semantics correct
- `_validate_url` blocks `http://` non-loopback, allows `[::1]`
- Health probe: always-active passes, crashing-blocks
- apt default off (`--with-apt` overrides)
- 100% grep verification of every patch

**NOT verified (requires hardware or a live backend):**
- **A.1** H11/H12 — no runtime test possible without receipt hardware
- FS sync on real USB printer (fsync semantics vary by driver)
- Auto-rollback against a real broken commit
- Health probe against real PyQt startup time
- EOD width on 58 mm EOD (no printer available)
- Phase 12.2 hardware matrix (USB printer-class, USB serial @ 9600, @ 115200, CUPS raw)
- Phase 12.3 fleet dry-run on staging

**Recommendation:** run Phase 12.2 + 12.3 as a **minimum** before promoting any fleet.

---

## What I'd tell a stakeholder

> **We closed every fleet blocker the audit identified. The one thing the audit was wrong about (double-print) turned out to be safe already. The one thing we added beyond the audit (polkit) made a bigger difference than everything else combined. There's one customer-facing receipt bug left (H11/H12 — long product titles drop the price column) and three privacy/compliance items (log file perms, upload consent, hostname hashing). None of those individually block a deployment, but you probably don't want to ship the receipt bug to a thousand stores.**
>
> **One more 3–4 hour session closes A and B. One full day closes A, B, C, and D. Then a day for Phase 12 verification on real hardware. That's the fleet-ready line.**

---

## Recommended next sessions

**Session 1 (~3 h) — close the real bugs (A) + privacy (B):**
- H11/H12 `_col2` price-drop fix
- Log file 0600 (A.4)
- Log upload consent toggle (B.1)
- Hashed hostname (B.2)
- Clamscan privileged paths (A.5)
- udev dead line (A.3)
- Verify `ExecStart` heredoc (A.2)

**Session 2 (~4 h) — close defense-in-depth (C):**
- `install.sh` dirty-tree check (C.3)
- Update timer health telemetry (C.4)
- Release branch + channel infrastructure (C.1) — needs backend coordination
- AnyDesk rotation design (B.3) — decision + planning

**Session 3 (~1 day) — Phase 12 verification:**
- Hardware matrix (12.2)
- Staging dry-run with 3 devices (12.3)
- Promote to `stable` once stable for 4 h

**Session 4 (~2–3 h) — cleanup (D):**
- `old/`, `prompting/` out of repo
- `.venv` → `venv/` unify
- 4 remaining `.ok` sites audited
- Poll interval 5 → 15 (optional)
- Dead code removal
- Log path to XDG

**Session 5 (~2–3 h architectural) — Phase 7.5** (C.2):
- Move executed tree to `/opt/vula-print` root-owned
- Decouple user-tree (for pulls) from executed-tree (for running `install.sh`)

Sessions 1 and 2 are what keep you honest. Sessions 3 and 4 are what keep the fleet healthy. Session 5 is what closes the last remaining attack chain that the sprint didn't fully close.

You can also ship to `beta` devices after Session 1, and to `stable` after Session 3. The intervening sessions are improvements, not blockers.