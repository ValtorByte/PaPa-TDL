# Contributing to PaPa-TDL

Use a non-root development user and Python 3.11+.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python -m playwright install chromium
PAPA_TDL_BROWSER_TEST=1 python -m unittest discover -s tests -v
```

The browser test uses a synthetic local HTML page. It does not sign into Telegram or Google. The bootstrap regression creates a real pip-less venv and uses pip's `--python` option to install a local test wheel without network access. Installer orchestration tests stub package/browser installation and simulate a failing ensurepip path.

Run `bash -n install.sh`, Python compilation, `git diff --check`, and the tests before opening a pull request. Keep secrets, browser profiles, OAuth JSON, tokens and downloads out of commits. `.env.example` is documentation, not an automatically sourced file.

Configuration lives under `TDL_DATA_DIR` (default `~/.local/share/papa-tdl` on Linux, `~/Library/Application Support/PaPa-TDL` on macOS and `%LOCALAPPDATA%\PaPa-TDL` on Windows for manual installs). Saved settings can be overridden by `TDL_DESTINATION=drive|local`, `TDL_OUTPUT_DIR`, `TDL_GROUP_NAME`, `TDL_POLL_SECONDS`, `TDL_MAX_DOWNLOAD_BYTES` and `GOOGLE_DRIVE_FOLDER_ID`. Terminal colors respect `NO_COLOR` and are removed from redirected output.

A fork can set `PAPA_TDL_REPO=YOUR_USER/PaPa-TDL` at installation. The canonical installer source is `ValtorByte/PaPa-TDL`; update README URLs when hosting your own fork. Do not make assumptions about private Telegram/Drive accounts in tests.

## Google sign-in for all users

Never commit Web OAuth secrets, raw JSON credentials, session files or user tokens. The maintainer explicitly approved distributing this project's Desktop installed-app client identity in _google_client.py. This narrow exception does not apply to user credentials or other OAuth clients. Configure Desktop OAuth privately on each host using DATA_DIR/google-client.json, readable only by the runtime user. The optional tools/configure_google_oauth.py utility saves to a private directory outside the repository; use --data-dir to select the runtime directory and run as its owner. It never writes credentials into source files. The distributed Desktop app identity is maintained separately and must not be obfuscated to evade secret scanning.

The resolver accepts --client-file or private DATA_DIR/google-client.json. Google endpoints and drive.file scope are fixed. Desktop callbacks on a VPS need SSH forwarding from the browser computer. Configure the consent screen audience and test users before login.
