# Changelog

## 1.6.9 — Unicode RAR names and numbered archive collisions

- Run archive extractors in a UTF-8 locale so Persian filenames keep their original names and do not collide as question marks.
- Extract independent `Part01`, `Part02`, and similar numbered archives into separate subfolders within one batch folder. Repeated names such as `photo.jpg` are preserved.
- Retry a retained failed batch after upgrading the extraction engine, without downloading the archive parts again.

## 1.6.8 — archive backfill and numbered sets

- Search older Telegram history when a later RAR volume is found first, so missing earlier volumes can join the same batch.
- Group archives named like `Course_Part01 (01-22).rar` and `Course_Part02 (23-43).rar` into one extraction folder, including batches retained from older releases.
- Keep the downloaded source parts until extraction and destination verification complete.

## 1.6.7 — terminal resize fix

- Redraw the interactive menu when the terminal width changes while it is open. A compact menu replaces wide bordered cards below 100 columns, and the banner shrinks below 55 columns.
- Exercise real PTY resizing at 40, 60, 80, 120 and 160 columns. This fixes the overlapping borders reproduced in the Google Colab terminal.

## 1.6.6 — prepared, not deployed

- Use a controlling terminal for native `unrar` password prompts. Hidden passwords are not passed in process arguments or stored by the new handoff path.
- Keep other files moving while an archive waits for a password, a part, or a retry. A completed archive no longer skips ordinary media found in the same scan.
- Treat Telegram Web's displayed file size as an estimate. Persist the exact number of downloaded bytes for recovery.
- Recheck local extracted files and Google Drive batch children before declaring a saved batch complete.
- Persist and verify the selected default Google Drive `TDL` folder ID.
- Add an explicit `papa-tdl share` command. Files remain private unless `--public` is confirmed; the printed direct-download URL is subject to Google's access, confirmation, and quota rules.
- Show recently verified account states in the terminal menu and remove a bundled personal Desktop OAuth identity from the current source.

### Upgrade notes

Existing browser sessions, Google tokens, settings, pending files and archive batches stay in the private data directory. Do not delete that directory during upgrade. Earlier plaintext `extract-password.json` handoffs are read once for migration; new passwords use a private local socket. Native password entry requires POSIX and an installed `unrar`; the documented manual install path applies on platforms outside the Ubuntu/Debian installer target.

Historical Git commits may still contain previously committed OAuth client details. Removing them from current source does not erase shared history; rotate credentials when appropriate. Do not bypass GitHub secret scanning or force-push an automatic history rewrite.
