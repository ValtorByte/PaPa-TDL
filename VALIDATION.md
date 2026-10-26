# PaPa-TDL 1.6.9 validation

## Automated checks

- `.venv/bin/python -m unittest discover -s tests -q` passed: 112 tests, including two opt-in cases skipped. The suite exercises local transfer, duplicate detection, recovery, hosted OAuth logic with mocked endpoints, Drive metadata and checksum checks with mocked APIs, terminal/PTY input, installer paths, archive grouping, Unicode extractor locales, repeated filenames across numbered archives, and real terminal resizing at 40, 60, 80, 120 and 160 columns.
- A later RAR volume appearing before earlier messages now triggers an older-history scan. Numbered independent archives such as `Course_Part01 (01-22).rar` and `Course_Part02 (23-43).rar` join one batch. A two-file numbered ZIP set was extracted into one local folder, and an older saved batch was migrated in tests.
- A real native encrypted RAR fixture is extracted using `unrar` with the correct password and rejected with an incorrect password. The test uses a controlling PTY, matching `unrar`'s prompt behavior. Multipart encrypted RAR and split 7z were also exercised locally with temporary synthetic archives; no personal files were used.
- Regression cases cover rounded Telegram Web sizes, a damaged saved extraction folder, a failed pending transfer followed by an independent success, an ordinary file arriving in the same scan as an archive completion, and private Drive links versus explicit public opt-in.
- `.venv/bin/python -m compileall -q src/tdl_watcher`, `bash -n install.sh`, and `git diff --check` passed.

## Live Colab recovery

- An authenticated Telegram group contained three retained numbered RAR archives. All three had downloaded completely (about 4.9 GB). The original `unrar` process used the `C` locale, replacing Persian filenames with `?` and failing with exit 9.
- Changing the extractor locale to UTF-8 opened the archive and preserved Persian names. A second extraction attempt exposed independent `photo.jpg` files in multiple archives. Separating numbered archives into `Part01`, `Part02`, and `Part03` subfolders resolved that collision. A full extraction of the retained real archives produced 70 files with the supplied password.
- The running Colab watcher was restarted with the same fix. It reused the retained downloaded archives, extracted 70 files, uploaded about 4.7 GiB to the private Google Drive `TDL` folder, and reported `Drive batch upload: 70 files verified`. This verification used the browser-based Telegram downloader; Telethon was not needed for this failure.

## Not verified in this checkout
- The hosted Google OAuth broker, public production consent and domain verification are external deployments and must be checked independently before promising ordinary users a working sign-in link.
- This revision has not been installed on hunt or another VPS. The user requested no Tailscale access, and no alternative authorized connection was used here. The Colab installation was patched in place for the live recovery; install version 1.6.9 from the repository for persistent deployment.
- GitHub CI, macOS, Windows and a fresh container install have not run for this revision. The installer targets Ubuntu/Debian; other systems use the documented manual path.

## Remaining limits

Telegram Web exposes rendered history and may miss messages during outages or bursts. Its layout can change. Protected media and account permissions are not bypassed. A background watcher has no attached password prompt; open the live console to answer it. Completed Drive files are private by default, and Google may apply confirmation or quota limits even to publicly shared direct links. Incomplete Drive uploads may restart from the beginning after process restart while the completed local source remains available for retry.

The current CLI uses an ANSI terminal menu and JSON recovery state. A Textual interface and SQLite migration have not been delivered in this revision. Do not present this revision as satisfying those parts of the product specification or as a verified live deployment.
