# PaPa-TDL 1.3.2 validation

Validated locally on Linux with CPython 3.12.15. The user-facing validation date is 2026-10-23 (Asia/Tehran).

## Passed

- A real pip-less venv created with `--without-pip`; pip's `--python` option installed a local wheel into it with no network or ensurepip bootstrap.
- Installer stubs simulate an ensurepip failure if the old venv creation path is attempted; both local and remote bootstrap installation still succeed with the new path.
- First-run defaults: Google Drive, automatic TDL folder selection, a prepared local default folder, optional deferred account login, missing release identity guidance and a real pseudo-terminal wizard/menu flow with purple ANSI output.
- Phone entry uses a visible prompt, accepts international and Iranian mobile formats, and keeps code/password prompts hidden. The terminal banner uses ANSI Shadow with a narrow-terminal fallback.
- The dimensional SVG wordmark was rendered in Chromium and visually inspected.

- 62 automated tests: `PAPA_TDL_BROWSER_TEST=1 python -m unittest discover -s tests -v`.
- Real Chromium: a controlled local Telegram-like HTML fixture, rendered-media discovery, native browser download event, complete file bytes and private pending path. No Telegram account was used by this fixture.
- Real CLI subprocesses: local setup with a custom absolute output directory, streaming direct-file download, repeated-source skip and final file content.
- Real Linux background processes: start/status/logs/stop without systemd or Docker, saved output settings, portable profile locking, graceful stop requests, browser cleanup and protection against signaling an unrelated PID.
- Real loopback HTTP server: interrupted response recovery using byte ranges and validators, changed/no validator restart, safe filenames, errors without signed URL disclosure, disk and size limits.
- Local storage: atomic hard-link save, cross-filesystem copy fallback with bounded memory, copy failure retaining the pending source, duplicate size conflicts, unsafe symlink rejection, private settings permissions and stale empty queue cleanup.
- Native installer scripts: local checkout and `bash -c SCRIPT @ install` bootstrap with a generated repository archive, isolated package/browser-install stubs, repeat installation preserving an existing token, active-profile update refusal and invalid repository rejection. Test stubs reject any systemctl or Docker invocation.
- Mocked Google browser OAuth: first-run/CLI default selection, PKCE, missing-JSON and callback-timeout guidance, private token file and verified Drive folder. Alternative device flow: link and code, pending/approved polling and token scope.
- Mocked Drive: app folder creation/reuse, preallocated stable IDs, size and MD5 checks, retention of the local file on verification failure.
- Real bundled Google Drive static discovery: upload/list/get/generateIds request parameters accepted; standard client transport excludes HTTP 308 from redirects. No Google API request was sent.
- Python compilation, Bash syntax and `git diff --check`.
- Source distribution and wheel built successfully with `uv build`.
- Terminal styling: purple ANSI on a terminal, no ANSI in pipes, NO_COLOR respected.

## Not performed

- Login to the user's Telegram account, real private/group media transfers or a live Google consent/upload.
- An actual apt/Chromium dependency installation on a clean VPS; native installer package installation was stubbed for its orchestration tests.
- Docker image build/run or a GitHub Actions run. Docker remains optional.
- Publishing or pushing changes to GitHub.
- A macOS or Windows machine run; GitHub Actions now has jobs for both, but those jobs have not run for this local change.

Before unattended use, install on the target VPS, run setup/login/check, transfer a small Telegram file, verify its bytes at the selected destination, restart the watcher and confirm duplicate skipping.

Telegram Web only exposes rendered messages and does not guarantee a full history or lossless capture during outages/bursts. Login revocation and saving restrictions cannot be bypassed. Background start survives SSH disconnection but not VPS reboot; arrange startup using the host's process manager if required. Incomplete Drive uploads can restart from byte zero after a process restart; completed local pending files are retained for that retry.

Release identity resolution, fixed Google endpoints, PKCE URL construction, private override precedence and failure before a false success were tested without a live Google account. Desktop OAuth configuration is kept outside source in private runtime storage. Live Google consent and upload remain unverified.
