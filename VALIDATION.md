# PaPa-TDL 1.6.8 validation

## Automated checks

- `python -m unittest discover -s tests -q` passed: 110 tests, including two opt-in cases skipped. The suite exercises local transfer, duplicate detection, recovery, hosted OAuth logic with mocked endpoints, Drive metadata and checksum checks with mocked APIs, terminal/PTY input, installer paths, archive grouping, and real terminal resizing at 40, 60, 80, 120 and 160 columns.
- A later RAR volume appearing before earlier messages now triggers an older-history scan. Numbered independent archives such as `Course_Part01 (01-22).rar` and `Course_Part02 (23-43).rar` join one batch. A two-file numbered ZIP set was extracted into one local folder, and an older saved batch was migrated in tests.
- A real native encrypted RAR fixture is extracted using `unrar` with the correct password and rejected with an incorrect password. The test uses a controlling PTY, matching `unrar`'s prompt behavior. Multipart encrypted RAR and split 7z were also exercised locally with temporary synthetic archives; no personal files were used.
- Regression cases cover rounded Telegram Web sizes, a damaged saved extraction folder, a failed pending transfer followed by an independent success, an ordinary file arriving in the same scan as an archive completion, and private Drive links versus explicit public opt-in.
- Run `python -m compileall -q src`, `bash -n install.sh`, and `git diff --check` before release.

## Not verified in this checkout

- No live Telegram arrival → archive extraction → Google Drive upload was completed for this revision. The connected Colab runtime restarted during manual terminal checks, so its temporary test copy and session were lost; the authenticated Telegram Web group was inspected read-only for real archive naming patterns.
- The hosted Google OAuth broker, public production consent and domain verification are external deployments and must be checked independently before promising ordinary users a working sign-in link.
- This revision has not been installed on hunt or another VPS. The user requested no Tailscale access, and no alternative authorized connection was used here.
- GitHub CI, macOS, Windows and a fresh container install have not run for this revision. The installer targets Ubuntu/Debian; other systems use the documented manual path.

## Remaining limits

Telegram Web exposes rendered history and may miss messages during outages or bursts. Its layout can change. Protected media and account permissions are not bypassed. A background watcher has no attached password prompt; open the live console to answer it. Completed Drive files are private by default, and Google may apply confirmation or quota limits even to publicly shared direct links. Incomplete Drive uploads may restart from the beginning after process restart while the completed local source remains available for retry.

The current CLI uses an ANSI terminal menu and JSON recovery state. A Textual interface and SQLite migration have not been delivered in this revision. Do not present this revision as satisfying those parts of the product specification or as a verified live deployment.
