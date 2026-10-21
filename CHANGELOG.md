# Changelog



## [1.1.875] - 2026-10-22

Sprint continuation. Closes every item from 1.1.874's "Known
outstanding" list — including H11/H12 (the receipt price-column bug
1.1.874 shipped unfixed), the log-file privacy items, and the
log-upload consent gap. Adds a diagnostic audit script for field use.
Rollback anchor: `pre-sprint-20261021`.

**Caveats up front:** hardware verification (Phase 12.2/12.3) still
has not been run. The receipt-renderer fixes are unit-tested and
verified against the algorithm, but have not been exercised against a
physical ESC/POS printer. Deploy to one staging device and run
`print_audit.sh` before promoting to fleet.

### Added
- `print_audit.sh` — read-only diagnostic collector. Gathers system,
  install state, printer devices, app state, update history, config,
  and logs into a single tarball with all credentials redacted. Auto-
  flags a summary of issues (stuck update counter, creds perms,
  log perms, timer state, missing origin/main, etc.). Ships to field
  via git; safe to email back. `--full` also captures `lsusb`/`dmesg`.
- `X-Client-Version` header on every `HttpWorker` request, sourced
  from `vula_config.APP_VERSION_SHORT`. Backend can answer "which
  devices are on which build?" from the poll log alone — no extra
  endpoint. `POSPollWorker` retains its own injection (direct
  `requests`); both paths carry the header.
- `_history_reprint_job` persistent reference on `VulaPrintApp` —
  the reprint worker's QThread wrapper now outlives the C++ thread.
  See Fixed below for the bug this closes.
- Busy-file self-heal in `vula-update.sh`. `/tmp/vula-print-busy`
  older than 3600 s is removed before proceeding; a crashed print
  no longer permanently blocks updates.
- Consecutive-failure tracker in `vula-update.sh`. Increments
  `/var/lib/vula/update-failure-count` on failure, resets on success,
  logs an ALERT at 3+. Surfaces silently-stuck devices — the previous
  update path had no way to distinguish "working, nothing to do" from
  "has been failing silently for weeks".
- `install.sh --no-log-upload`. Writes `/etc/vula/log-upload-disabled`
  (root-owned, 0644). App refuses to upload logs while present;
  override requires `sudo rm`. Deliberately separate from the UI
  toggle so a customer who declined at install cannot be silently
  opted in by a later UI action.
- Dirty-tree check in `install.sh`. Refuses to run if `install.sh`,
  `uninstall.sh`, or `system/*` are dirty in git. Closes the gap
  between `vula-update.sh --local` (which already refused) and direct
  `sudo bash install.sh` (which did not). `--force-install` bypass
  documented as the deliberate escape hatch.
- Query-timer state in `install.sh` summary. Replaces a bare `list-
  timers` line with per-timer enabled/active status and a description.
  An installer that silently failed to enable `vula-print-update.timer`
  is now flagged red in the summary instead of "all phases completed".
- `desc_budget - 1` ellipsis in `POSSlipPrintJob._build_receipt_bytes`
  item titles. Truncated titles now end in `…` instead of mid-character.

### Changed
- **`_col2` rewritten to fit-with-ellipsis** in both `POSSlipPrintJob`
  and `POSEODReportPrintJob`. The previous version produced a line
  wider than `width_chars` when `len(left) + len(right) + 1` exceeded
  it, which `_txt` then truncated from the right — silently chopping
  the money column off the receipt. The new version ellipsizes the
  left column to make room for the right and never drops a price.
  See Fixed below for the bug context.
- `_col2`, `_line_sep`, and `_txt` in both receipt workers now truncate
  on the fitted `_col2` output, not the raw pre-join string. A 32-char
  58 mm receipt with a `R 123456.78` total prints intact.
- **Log file chmod 0600 at open**, both fresh-install and legacy-upgrade
  paths. `setup_logging` chmods the directory to 0700 and the file to
  0600 idempotently; a pre-existing 0664 file from a prior install is
  tightened on next launch. Logs contain device fingerprints and
  backend 4xx response snippets.
- **Auto-updater now runs `vula-update.sh`, not
  `vula-print-update.sh`.** The latter had no health probe, no rollback,
  and swallowed `pip install` failures — exactly the safety net 1.1.874
  added to `vula-update.sh` but never wired to the timer. The daily
  service now runs the path that self-rolls-back on failure.
  `vula-print-update.sh` is retained for one release cycle for manual
  callers and marked deprecated in its header.
- `install.sh` phase order: `update_helpers` runs before `updater`.
  The new `vula-print-update.service` points at `vula-update.sh`, which
  is installed by `update_helpers`; reordering ensures a
  `--phase=updater`-only run finds the binary.
- App-update timer jitter increased from 5 m to 30 m. At 1000+ devices,
  5 m meant the fleet converged on a single window — no time to abort a
  bad commit before it hit everything. 30 m spreads rollout at zero
  cost per-device.
- `_on_exit` trap in `vula-update.sh` returns the original `$_rc`
  explicitly. An `if` body that runs on the 3rd+ failure was implicitly
  returning 0 — the shell would exit 0 despite a failed update, hiding
  the exact failure the alert was trying to surface.
- `--check` mode in `vula-update.sh` no longer resets the failure
  counter. Diagnostic runs by an operator investigating a stuck device
  were erasing the evidence of how long it had been stuck.
- Busy-file check gains per-print "age ≥ 3600 s" self-heal. Without
  it, a single SIGKILL'd print meant the device never updated again.
- `install.sh` summary prints per-timer descriptions and severity:
  green ✓ for active, yellow ! for enabled-but-inactive, dim ○ for
  not-enabled. The single "auto-update WILL NOT run" line fired on
  the AnyDesk handshake timer during a `--phase=preflight`-only run,
  which is both wrong (AnyDesk ≠ auto-update) and alarming for a
  legitimate partial-phase install.
- `LogUploadCoordinator` suppression is now **per-connection**, keyed
  by `connection_id`, not fleet-wide. One store returning 404
  (backend endpoint not deployed) no longer silences uploads to a
  healthy store on the same device.
- Clamscan target list expanded from `$REAL_HOME` only to
  `$REAL_HOME` + `/usr/local/bin` + `/usr/local/sbin` + `/etc/vula`.
  A malware drop in a root-run script (e.g. a hook into
  `vula-update.sh`) was previously scanned by nothing.

### Fixed
- **Reprint crash.** `_reprint_by_job_id` created the reprint worker
  as a local variable with no persistent reference. When the slot
  returned, Python dropped the last reference to the QThread wrapper
  while the C++ thread was still running — Qt calls
  `qFatal("QThread: Destroyed while thread is still running")` and
  the whole app aborts. Symptom: click Reprint in History tab, UI
  drops, manual restart required, reprint never completes. Fix keeps
  a persistent `self._history_reprint_job` and adds a busy guard
  against double-click. This bug was not in the original audit.
- **History dialog reprint overwrites a running print.** Same class
  of bug as above, on `_on_reprint_detail_fetched` in `vula_ui/history.py`.
  Guarded by `_thread_alive(self.print_job)` check before assigning.
- **`_start_label_print_from_cached` overwrites a running print.**
  Same class again, on the label-print path in
  `vula_ui/actions.py`. Guard runs *before* `mark_printing()` so a
  lost CAS is not left stuck in `printing` status.
- **`.isRunning()` on deleted C++ objects** replaced with the safe
  `_thread_alive()` helper in 4 slots: `_on_poll_slip_ready`,
  `_on_poll_eod_slip_ready`, `print_test_pos_slip`, `calibrate_printer`.
  A previous thread that silently died left the wrapper pointing at a
  destroyed C++ object; `.isRunning()` raised `RuntimeError` inside a
  slot, which the excepthook logged but the Qt event loop continued
  past — leaving the "A POS slip is already printing" busy state
  permanently stuck.
- **H11/H12 receipt price-column corruption.** `desc_budget` reserved
  14 chars for the price but a `R 1234.56`-style total is 9; a wide
  title + qty prefix + line total could exceed `width_chars` and
  `_txt` truncated the price. Now `_col2` guarantees fit by ellipsizing
  the left column (never the price) and `desc_budget` reserves 12 with
  an explicit `…`. Verified against a synthetic 32-char test: all
  wide-money lines fit and end in the intact price string.
- **Log upload fleet-wide suppression.** `_suppress_until_ts` was a
  single scalar; one store's 404 silenced uploads to every store. Now
  keyed by `connection_id`. Multi-store devices keep uploading to
  healthy backends during a partial-outage.
- **Clamscan excludes never matched.** `clamscan --exclude-dir` matches
  basenames, not paths. `^/proc`, `^/sys`, `^/dev`, `^/run`, and
  `.config/vula_print` were syntactically valid and completely dead.
  Removed; only the basenames (`venv`, `.cache`, `.mozilla`) that
  actually fire are kept.
- **udev rule `usb` line was dead.** Removed. Only `usbmisc` fires on
  modern kernels; the parallel `usb` rule was cosmetic. A future
  maintainer reading the rule file is no longer misled.
- **`install.sh` header summary pointed at the deprecated command.**
  Now prints `sudo /usr/local/bin/vula-update.sh`.
- **`vula-print-update.service` `ExecStart=` referenced
  `vula-print-update.sh` with `\${VAR}` args.** Replaced with an
  unparameterised call to `vula-update.sh` (which sources install.env
  itself). Eliminates the word-splitting hazard structurally rather
  than by escaping.

### Security
- No new security surface introduced. The log-upload consent toggle
  and the install-time hard-off file add an opt-out that did not
  previously exist; they do not weaken any existing access control.
- The dirty-tree check in `install.sh` restores the invariant that
  `vula-update.sh --local` already enforced: privileged files cannot
  be promoted to root paths from an uncommitted working tree.
- Version reporting via `X-Client-Version` is non-sensitive (public
  release number) and reachable only on already-authenticated
  requests.

### Known outstanding
- **N72** — `vula-update.sh` merge step falls back to `origin/master`
  on repos that only have `origin/main`. Manifests on this dev box
  as "origin/master - not something we can merge" and a silent 13-day
  update failure; would not affect fresh clones. Fix (SHA-validated
  merge, `rev-parse --verify` per candidate, per-branch logging) is
  designed and ready; must be applied and tested on one clean-clone
  device **before** any stable fleet rollout.
- **N70** — creds.env self-heal is in `vula-update.sh` (recovers from
  `print_jobs.db` conn_1 if the file is missing). Exercised by `--check`
  mode only on `--check` exit, so has not been end-to-end verified
  against a full update. Will self-verify on the next real cycle.
- **N71** — `vula-update.sh` health probe is a fixed 30 s window
  requiring 15 s consecutive. Adequate for the PyQt app's ~2 s boot
  time; may be tuned down in Phase 4 to reduce fleet update time.
- **Design ready, not implemented** — Phase 4.2 force-update
  (backend `GET /update-check` + 6 h timer), Phase 4.3 release
  channels (`main` → `beta` → `stable` with backend promotion),
  Phase 4.4 fleet version dashboard (backend work only, client
  already reports via `X-Client-Version`).
- **Hardware verification** — Phase 12.2 (USB printer-class, USB
  serial @ 9600/115200, CUPS raw, 58 mm ESC/POS, 80 mm ESC/POS) and
  Phase 12.3 (3-device staging dry-run) still not exercised against
  physical hardware. `print_audit.sh` is the collection tool; run it
  on one real client and ship the tarball before any fleet rollout.

## [1.1.874] - 2026-10-22

Sprint: print-reliability + security hardening. Closes every fleet-blocking
finding from the 2026-10-21 audit. Rollback anchor: `pre-sprint-20261021`.

**Caveats up front:** (a) the H11/H12 receipt-column bug (long product title
truncates the price column) was on the Phase 2 scope list but did not land
— see "Known outstanding" below. (b) Hardware verification (USB printer-class,
USB serial, CUPS raw) has not been run against these changes. Deploy to a
staging device before promoting to fleet.

### Added
- `HttpResult.transport_ok` field distinguishes "reached the backend"
  (transport succeeded) from "backend accepted this" (2xx). Existing
  `.ok` becomes a property returning `transport_ok and 2xx`. Callers
  that meant "did the request get through at all" migrate to
  `transport_ok`; callers that meant "did it succeed" stay on `.ok`.
  Fixes the original footgun where a 500 reported `ok=True` and
  completion retries were silently discarded.
- `HttpWorker` response body cap (`MAX_RESPONSE_BYTES = 10 MB`) enforced
  via `stream=True` + `iter_content(65536)`. A compromised backend
  returning a multi-GB body no longer OOMs the POS terminal.
- `HttpWorker` split connect/read timeouts (`(5.0, 15.0)`); an idle
  backend is now detected in 5 s regardless of per-request timeout.
- `HttpWorker` Content-Type aware response decoding. `200 text/html`
  (captive portal, backend bug) no longer gets parsed as JSON and
  crashes the caller on `.get()`.
- `PrintStore._secure_db_permissions()` chmods `print_jobs.db`,
  `-wal`, and `-shm` to 0600 after WAL is enabled. Fixes the audit C3
  exposure where the API key was readable by any local user under the
  default `umask 022`.
- `settings.json` is chmod 0600 on save, and on load as an upgrade path
  for existing installs. Contains the same API key as the DB.
- `POSSlipPrintJob._logo_bytes` SSRF guard: origin allowlist matches
  the connection's `api_base_url` (scheme + netloc), 2 MB body cap,
  20 MP `PIL.Image.MAX_IMAGE_PIXELS`, PNG/JPEG/GIF format whitelist,
  `img.verify()` before render. Fixes audit C9 and the earlier N24
  bug where `_download_brand_logo_async` had the same exposure at
  60 s cadence.
- `_on_eod_ensure_attempted` slot + `eod_ensure_attempted` signal,
  hourly gate on the EOD `/ensure-latest` POST via
  `StoreConnection.last_eod_ensure_at`. Idle clients no longer send
  ~12 k POSTs/min to the backend at 1000-client scale.
- `health_check()` in `vula-update.sh` — requires the app to have been
  continuously active for ≥15 s within a 30 s window after restart,
  catching both "immediately crashes" and "crashes after a few
  seconds" failure modes.
- Auto-rollback in `vula-update.sh` — on health probe failure, git
  resets to `PRE_SHA`, re-runs `install.sh`, re-runs the health probe,
  logs the bad commit for review, and only promotes to
  `last-known-good` on success. The LKG file the script has always
  written is now actually consulted.
- `--with-apt` flag on `vula-update.sh`. `--no-apt` (apt phase off) is
  now the default so fleet auto-updates don't run `apt-get upgrade` on
  every device at 04:05.
- `--allow-http-dev` installer flag: permits `http://` for non-loopback
  dev installs. Refused when combined with `--yes` so the fleet
  auto-updater can never silently downgrade an install to cleartext.
- `uninstall.sh` removes `/etc/polkit-1/rules.d/49-vula-update.rules`
  and reloads polkit. Previously the rule survived uninstall, leaving
  an orphan grant on an otherwise-uninstalled box.

### Changed
- **Polkit `49-vula-update.rules` reverted to a hard no-op.**
  The rule previously granted `subject.local` (any local user)
  permission to start `vula-update-{local,manual}.service` **without
  a password**. Combined with the root-execution of
  `$SOURCE_DIR/install.sh` this was a one-line local→root escalation
  available to any store clerk with a terminal — audit C13. Default
  polkit policy now applies; an admin password is required.
- Both update systemd services gained defence-in-depth sandboxing:
  `NoNewPrivileges=true`, `ProtectSystem=strict`, `PrivateTmp=true`,
  `ProtectKernelTunables/Modules/ControlGroups=true`, and an explicit
  `ReadWritePaths=` allowlist for the installer's legitimate write
  paths. `ProtectHome` is deliberately left at its default because
  `install.sh --phase=print_app` writes `$SCRIPT_DIR/venv/` under the
  user's home — a `read-only` setting would have broken the updater
  (this was in the audit's recommended fix and would have caused a
  fleet-wide outage).
- `vula-update.sh --local` refuses to run `install.sh` if any
  privileged-install path (`install.sh`, `uninstall.sh`, `system/*`)
  is dirty in git. App-code dirty changes remain permitted — `--local`
  exists specifically to deploy local app fixes without pushing, so
  refusing any dirty tree would have defeated its purpose.
- `vula-print-update.service` uses `EnvironmentFile=` + `${VAR}`
  expansion in `ExecStart=` instead of positional `$REAL_USER
  $SCRIPT_DIR`. Fixes audit H20 (systemd word-splitting on paths with
  spaces). `vula-print-update.sh` falls back to
  `${REAL_USER:-}`/`${SOURCE_DIR:-}` so manually-invoked calls remain
  compatible.
- `vula-update.sh` captures `PRE_SHA` before git pull and `NEW_SHA`
  after, explicitly tracking the rollback target across the update
  rather than relying on the pre-existing LKG file which was never
  read.
- `install.sh::phase_print_app` DB seed upserts `conn_1` when
  credentials change, instead of skipping when any connection exists.
  Fixes audit M27 — a fleet-wide API key rotation now propagates to
  `print_jobs.db` instead of leaving the app on the old key while
  `/etc/vula/creds.env` had the new one.
- `POSPollWorker` no longer bails when `printer_roles.pos_slip` is
  unassigned. Polling continues (backend sees activity), prints are
  still gated on the role. Fixes audit H28 — a mis-assigned store no
  longer reads as "device is dead" on the fleet dashboard.
- `POSPollWorker.do_eod_ensure` new parameter; `poll_pos_slips`
  computes the hourly gate and passes it through.
- `_on_pos_print_finished` triggers an immediate re-poll on success
  (still CAS-guarded via the in-flight set), draining the queue at
  ~40 s for 20 slips instead of 100–300 s under the previous
  one-slip-per-5-s-tick cadence.
- `_on_poll_eod_slip_ready` gains the same `last_successful_pos_print_at`
  heartbeat and backoff reset as `_on_poll_slip_ready`. EOD-only
  stores no longer appear as "no recent print" on health dashboards.
- `_on_pos_complete_done` / `_on_eod_complete_done` explicit
  status-classified handling:
    * 200 / 204 / 404 → terminal, discard
    * 400 → log + discard (bug in the request, not retryable)
    * 5xx / network → retry, capped at `_POS_RETRY_SET_MAX = 500`
  per connection, oldest dropped on overflow. `while` loop drains the
  set into range rather than silently allowing it to grow.
- `_RetryFlushWorker` sleeps 250 ms between requests. A client that
  accumulated hundreds of retry ids during a backend outage no longer
  hammers the backend in one burst on recovery.
- `POSEODReportPrintJob` accepts a `width_chars` parameter (default
  48) with the same truncation semantics as `POSSlipPrintJob`. Fixes
  audit C12/H17/H18 — 58 mm EOD reports no longer wrap mid-column at
  the firmware level, misaligning totals.
- `vula_app.py` store open failure is now a hard fail: QMessageBox
  with the DB path, then `SystemExit(1)`. There is no fallback to
  `self._store = None` — printing without the audit trail (and
  without the CAS guard against duplicates) is worse than refusing
  to start. Fixes audit H25.
- `mark_interrupted_on_startup` returns `List[int]` of job IDs rather
  than a count; `vula_app.py` logs them. Operators investigating a
  stuck `printing` row now have the ID without querying the DB.
  Fixes audit L21.
- `_validate_url` in `install.sh` requires `https://` for non-loopback
  URLs. Loopback (`localhost`, `127.0.0.1`, `[::1]`) and mDNS
  (`*.local`) are still permitted for dev, since a store LAN can be
  trusted for those addresses. The API key header and the AnyDesk
  password are no longer eligible to traverse cleartext on a shared
  network. Fixes audit C4.
- The AnyDesk handshake script refuses to POST if
  `PRINTER_API_BASE_URL` is not `https://`, even if a user has
  manually edited `/etc/vula/creds.env` to bypass the installer check.
  Defence in depth — the handshake payload contains the password that
  grants remote-control access to the box.
- `_col2`, `_line_sep` and `_txt` in `POSEODReportPrintJob` are now
  instance methods reading `self.width_chars` instead of `@staticmethod`
  with a hardcoded `width=48`.
- `install.sh::phase_anydesk` documents the UFW decision
  (audit M3): the AnyDesk ports must stay open, the mitigation
  strategy for password reuse / no-MFA is a Phase 10 design task,
  and the comment explicitly warns future maintainers not to
  silently remove them to "harden" the firewall.

### Fixed
- **Silent print on USB.** `_write_file` now calls `os.fsync(fd)`
  after `flush()` and probes the file descriptor non-blockingly for
  EOF after a 50 ms settle window. A device that closed mid-write
  (printer unplugged, USB reset) is now detected and the write
  raises instead of returning success. The `PrintJob.completed`
  signal fires `(False, ...)`, the DB row goes to `failed`, and
  `/complete` is **not** POSTed. Previously the system reported
  success based on having handed bytes to a kernel buffer, even
  when the printer was out of paper, cover-open, offline, or
  disconnected.
- **Serial write timeout scaling.** `_write_serial` computes
  `write_timeout = max(5.0, (len(data) * 10 / baud) + 5.0)` instead
  of a fixed 5 s. At 9600 baud, a 30 KB receipt (logo + QR) takes
  ~30 s on the wire; the old fixed timeout fired mid-receipt and
  truncated output. Fixes audit H10.
- **ESC/POS injection.** `POSSlipPrintJob._txt` and
  `POSEODReportPrintJob._txt` reject C0 controls (`0x00–0x1F`),
  DEL, and C1 controls (`0x80–0x9F`). Previously a `\x1b@` in a
  product title reset the printer; `\x1dV\x41\x00` fired the
  cutter; `\x1bp\x00\x19\xfa` opened the cash drawer. Any
  backend- or user-supplied field was a potential command-injection
  vector. Fixes audit C8.
- **TSPL injection via newline.** `PrintJob._tspl_escape` strips
  bytes outside `0x20–0x7E` and space, then escapes `\` and `"`.
  TSPL is line-oriented — a `\n` in `variant_label`, `sku`, or
  `code39` terminated the string and started a new command. Fixes
  audit C15/M12.
- **Non-ASCII characters in receipts.** An intermediate revision of
  the ESC/POS fix stripped everything ≥ `0x7F`, turning "Café"
  into "Caf" — silent character loss on a tax-relevant receipt.
  The final fix preserves the character through the control-byte
  filter, then lets `encode("ascii", errors="replace")` degrade
  it to a visible `?`. `"Café"` prints as `"Caf?"`, not `"Caf"`.
- **Code39 barcode corruption.** `_generate_label_tspl` uppercases
  and filters SKU/code39 through the Code39 alphabet
  (`0-9 A-Z - . space $ / + %`). Invalid characters silently
  produced garbage or truncated barcodes on the printer. Empty
  after sanitisation raises `ValueError`, which `PrintJob.run`
  catches per-item and reports in the completion summary. Fixes
  audit M13/N14.
- **`PRINT 1` → `PRINT 1,1`.** TSPL interpreters vary on whether
  single-argument `PRINT` is accepted. The two-argument form is
  universally correct. Fixes audit M14.
- **Footer word-splitting.** `_build_receipt_bytes` uses
  `textwrap.wrap` instead of character-boundary slicing. "free
  deliv"/"ery on orders" no longer appears on receipts. Fixes
  audit M16/N15.
- **`_on_test_all_done` unbound local `ok`.** Any failing
  connection test raised `NameError` in a Qt slot; the excepthook
  swallowed it and the status bar froze on "Testing N connection(s)…"
  forever. The failure branch now sets `ok = False` explicitly.
- **`SIDEWBAR_W` typo in `_on_brand_logo_done`.** A misspelled
  attribute (`self.SIDEWBAR_W` instead of `self.SIDEBAR_W`) raised
  `AttributeError`, which the surrounding `except Exception: pass`
  swallowed. The brand logo has never rendered on a live fetch
  since this code shipped. Fixed, along with load-before-save
  validation (audit N19) that prevents persisting a non-image body.
- **Dead `_complete_pos_request` / `_complete_pos_eod_request`**
  removed from `vula_ui/pos.py`. Both were defined, neither was
  called. If anyone had revived them they would have bypassed the
  HttpWorker hardening. Unused `requests` import removed from the
  same module.
- **Log uploader accepts any 2xx.** `_on_done` previously required
  `status == 200`; a `204 No Content` (a valid upload success) fell
  through to the transient-failure branch and logged as an error.
- **`_on_config_fetch_done`, `_on_label_pending_fetched`,
  `_on_request_details_fetched`** classify transport failure vs.
  server response correctly. Under the new `ok` semantics, a
  backend 500 was rendering as "Connection failed" with an empty
  error string; these call sites now use `transport_ok` and
  surface the actual HTTP status.

### Security
- Local → root privilege escalation (audit C13/C14/C18) closed at
  three layers: (1) the polkit rule is now a hard no-op — the
  primary vector; (2) both update services are sandboxed with
  systemd directives and explicit `ReadWritePaths` allowlists; (3)
  `vula-update.sh --local` refuses to run if a privileged-install
  path is dirty in git. A second-compromised user cannot simply
  edit `system/bin/vula-update.sh` and start the service.
- API key in `print_jobs.db`, `-wal`, and `-shm` is chmod 0600 after
  every open (`_secure_db_permissions`). Idempotent — corrects
  pre-existing `0644` files on the first launch after upgrade.
- `settings.json` (which carries the same API key) is chmod 0600
  on save and corrected on load for legacy installs.
- AnyDesk handshake refuses cleartext `http://` at the point of
  transmission, even if `creds.env` is manually edited to bypass
  the installer's URL validation.
- `https://` required for the API base URL and the Firefox homepage
  URL. Loopback and `.local` dev exceptions remain. The
  `--allow-http-dev` override is refused in `--yes` mode so the
  auto-updater cannot silently downgrade an install.
- Auto-updater self-rolls back a bad commit within ~30 s of the
  health probe detecting a failed restart, and records the
  rolling-back SHA in the log. Fleet-wide blast radius of a bad
  merge to `main` is bounded by the number of devices that polled
  before the rollback landed, not by "all 1000 clients within 24 h"
  as before.
- UFW AnyDesk decision documented inline (audit M3) so a future
  maintainer reading the firewall rules does not "helpfully" remove
  the ports.

### Known outstanding
Listed here so they aren't lost. None individually block
deployment, but they were on the sprint plan and did not land:

- **H11/H12** — `POSSlipPrintJob._col2` can drop the price column
  on 32-char receipts when the item title exceeds `desc_budget`.
  `desc_budget` reserves 14 chars; a price like `R 1234.56` is 9
  chars, so a wide line ends up longer than `width_chars` and
  `_txt` truncates the price off. Customer-facing corruption on
  receipts with long product names.
- **A.4 / M20 / L33** — log file at
  `~/.config/vula_print/logs/vula-print.log` is still created under
  the process umask (observed `0664` on the dev box). Not wrapped
  in `os.umask(0o077)` + `chmod 0o600` yet.
- **B.1 / M1 / M36** — log upload has no user consent / opt-out
  toggle. Startup + 6 h periodic + crash-triggered uploads proceed
  unconditionally.
- **B.2 / M35** — `client_hostname` sent in the log upload payload
  is raw; no hashing. Correlatable with the store's API key on the
  backend.
- **H8** — clamscan still scans only `$REAL_HOME`. Malware dropped
  into `/usr/local/bin` (e.g. a hook in `vula-update.sh`) is not
  scanned — wrong scope for the remote-support + auto-updater
  threat model.
- **C2** — udev rule still contains the dead `SUBSYSTEM=="usb"`
  line. Only the `usbmisc` line fires on Debian 13. Harmless but
  confusing.
- **Hardware verification** — Phase 12.2 (USB printer-class, USB
  serial @ 9600, USB serial @ 115200, CUPS raw) and Phase 12.3
  (3-device staging dry-run) were not run. The fixes are correct
  in code and unit-tested; they have not been exercised against
  physical hardware since they landed.

  
## [1.1.873] - 2026-10-11

### Added
- Persistent print-job store (`vula_store.py`): SQLite-backed, stdlib
  only, records every label / POS slip / EOD job the app has fetched
  or attempted to print. Authoritative for history and the label
  auto-print queue.
- Inline History tab (replaces the placeholder that only opened the old
  dialog). Filter chips for All / Labels / POS Slips / EOD / Failed /
  Interrupted, a per-job detail panel, and a Reprint button that reads
  the cached detail from the store.
- Label auto-print toggle on the Label printer card. When on, queued
  label requests drain automatically after each queue refresh. Pauses
  itself on first failure and surfaces the failed job in History.
- `store_connections` table in `print_jobs.db` - connections now have a
  single authoritative home, replacing the two-place system across
  `.env` and `settings.json`.

### Changed
- Connections migrated from `.env` / `settings.json` to the SQLite
  store. Migration runs on first launch after the update:
  `settings.json` wins if it has connections, otherwise a legacy
  `.env` is read and renamed to `.env.migrated`. Idempotent - no-op
  on every launch after the first. No client needs to re-connect.
- Label detail payloads are fetched once per request (during the
  pending-queue poll) and cached. Clicking Print reads from disk
  instead of making a fresh network round trip - visible as the
  removal of the second `GET /label-printing/request/{id}` per click
  on slow links.
- POS and EOD job details are persisted the moment they arrive from
  the poll worker, before printing begins. A reprint is now possible
  even if the backend has already deleted the request.
- `install.sh:phase_print_app` no longer writes `$SCRIPT_DIR/.env`.
  Connections are seeded directly into the SQLite store instead.
  `/etc/vula/creds.env` (used by system services, not the desktop
  app) is unchanged.

### Fixed
- Duplicate-print race between the manual Print click and the
  auto-print drain. `PrintStore.mark_printing` is a compare-and-set
  on the job's status column - exactly one path wins, the loser
  bails silently.
- Interrupted prints (app crash mid-job) are swept on next startup:
  any row left in `printing` becomes `interrupted` with an event
  logged. Never auto-resumed - a duplicate POS slip is worse than a
  missed one; the operator can reprint from History.

## [1.1.872] - 2026-10-11

### Added
- `uninstall.sh --dry-run` now produces truthful output: every
  destructive operation is echoed with a `DRY:` prefix and success
  markers are downgraded to a dim `(dry-run) ...` note, so a preview
  never claims to have changed the system.
- `uninstall.sh --help` now shows the full header, including the
  "What this does NOT remove" and "Exit codes" sections.
- Dry-run banner is distinct: "Dry run complete - nothing was changed"
  instead of "Uninstall complete".

### Changed
- `uninstall.sh` reads `REAL_USER` and `SOURCE_DIR` from
  `/etc/vula/install.env` (current convention, written by `install.sh`
  since 1.1.87), with a fallback to the legacy `/etc/vula/source-dir`
  for older installs. `SUDO_USER` resolution order is now
  `SUDO_USER -> install.env -> loginctl -> script dir`, which is
  reliable when nobody is logged in at uninstall time.
- `uninstall.sh --nuke` now fails fast with a clear error when not run
  as root, instead of silently failing to remove the source directory.
- `uninstall.sh` internal cleanup: single-element `for` loop collapsed,
  `ok "  ..."` messages tidied (colour prefix already provides the
  spacing).
- `StoreConnection.from_settings_dict` now generates a stable
  `uuid4().hex[:8]` connection ID for records saved without one.
  Previously it used `id(data)` - the dict's memory address - which
  changed on every launch, silently resetting per-connection state
  (`pos_in_flight_ids`, `pos_backoff_until`, `pos_completion_retry_ids`)
  on every restart.

### Fixed
- **`vula_app.py` composition restored.** `SidebarMixin` was dropped
  from both the import block and the class bases during an unrelated
  refactor, causing
  `AttributeError: 'VulaPrintApp' object has no attribute '_build_sidebar'`
  at startup from `content.py:81`. Both references restored.
- **Dangling `HeaderMixin` reference removed.** `vula_app.py` imported
  `vula_ui.header.HeaderMixin` and listed it in the class bases; neither
  the module nor the class existed. Both references removed - nothing
  depended on them.

### Removed
- `vula-update.h` - a stale draft (5 minutes older than the live
  `system/bin/vula-update.sh`, and reading the wrong env file,
  `/etc/vula/source-dir`, instead of `/etc/vula/install.env`). Moved to
  `old/archive/vula-update-v1.sh` for history.

## [1.1.871] - 2026-10-09

### Added
- CUPS backend (`vula_printer_target.py`) with two modes per queue:
  `cups:<queue>:raw` (custom bytes, TSPL/ESC-POS passthrough) and
  `cups:<queue>:driver` (generic driver, print-ready documents).
- CUPS queue discovery merged into `PrinterScanner` via `lpstat`.
- Printer role dialog labels CUPS entries "custom / raw bytes" vs
  "generic / driver" so operators pick the right transport.
- `VERSION` file + `_read_app_version()` - version no longer shells
  out to `git`, works on deployments without a `.git` directory.
- `DEFAULT_POS_POLL_INTERVAL_SECONDS` - single source of truth for the
  POS poll interval (previously disagreed between two modules).
- `system/` tree: `vula-update.sh` orchestrator, polkit rule, and
  systemd units for manual and local updates.
- `isolated_anydesk.sh` - standalone AnyDesk setup wizard for
  KDE/SDDM installs.

### Changed
- Printer roles refactored to a single `printer_roles: Dict[str, Optional[str]]`
  keyed by role. Eliminates the dual `selected_printer` /
  `last_selected_printer` state that caused cold-boot printing failures.
- `uninstall.sh` now tears down every system-scope unit, script, and
  config directory written by `install.sh`. Adds `--nuke`,
  `--keep-config`, `--dry-run`.
- `install.sh` no longer blanks `/etc/apt/sources.list` unconditionally -
  only when active `deb` lines would conflict, and always with a
  timestamped backup.
- `calibrate_printer()` uses `write_to_device()` so it respects the
  per-device lock and pyserial configuration.
- `API_BASE_URL` no longer defaults to a hardcoded tenant URL - empty
  string by default, refuses to configure if unset.
- udev rule now matches both `usb` and `usbmisc` subsystems (Debian 13
  routes USB printer-class nodes through `usbmisc`).

### Fixed
- Print button silently failed with "No Printer" after a normal cold
  boot - `selected_printer` was never restored from `last_selected_printer`.
- A4 role assignment was never persisted to `settings.json`.
- `_uptime_seconds()` returned time-since-boot, not time-since-process-start.
- `_RetryFlushWorker` and `StoreConnection` were type-annotated in
  `vula_app.py` but never imported.

### Security
- `install.sh` backs up `sources.list` and `debian.sources` before any
  rewrite.
- `uninstall.sh` verifies no `vula-*` units remain registered after a run.
