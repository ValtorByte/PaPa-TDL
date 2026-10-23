# PaPa-TDL product pages

Buildless English pages for Google OAuth branding. No cookies, JavaScript, external fonts or analytics. The product pages contain no Google client secret, tokens or Telegram sessions. This is a product website, not an OAuth relay or hosted downloader.

## Files

- `index.html`: product, installation instructions, permissions and limitations.
- `privacy/index.html`: actual self-hosted data flows and account controls.
- `terms/index.html`: acceptable use, responsibilities and MIT license reference.
- `style.css`: shared responsive stylesheet.

## Publish on hunt

Copy `site/` and `ops/deploy-site-hunt.sh` together, preserving their relative paths. Run as an administrator on hunt:

```bash
sudo bash ~/papa-tdl-publish/ops/deploy-site-hunt.sh
```

The script serves `/papa-tdl/` through a separate static Nginx location on the existing `valtorbyte.ir` HTTPS virtual host. It uses a versioned web root, saves the previous config under `/var/backups/papa-tdl-site/`, checks Nginx syntax, and restores previous config if publication fails. It does not restart or rebuild the existing application. Review the script before executing it. It requires the existing `/var/www/valtorbyte.ir/ops/nginx.locations.conf` include and a running Nginx master.

After deployment verify all three URLs return the intended HTML over HTTPS, not the main site's fallback page:

- https://valtorbyte.ir/papa-tdl/
- https://valtorbyte.ir/papa-tdl/privacy/
- https://valtorbyte.ir/papa-tdl/terms/

## Google Cloud setup

In the `papa-tdl` project, Google Auth Platform → Branding:

- App name: `PaPa-TDL`
- Support/contact email: `thevaltorbyte@gmail.com`
- Homepage: `https://valtorbyte.ir/papa-tdl/`
- Privacy policy: `https://valtorbyte.ir/papa-tdl/privacy/`
- Terms: `https://valtorbyte.ir/papa-tdl/terms/`
- Authorized domain: `valtorbyte.ir`

Verify domain ownership using Google Search Console with the same account. Check Data access declares only `https://www.googleapis.com/auth/drive.file` for Drive. Review any required disclosures, publish under Audience, and complete any branding review the Verification centre requests. Publishing is not a guarantee of Google verification. Do not add broader Drive scopes merely to resolve a review problem.

The app's Desktop OAuth callback on remote machines still requires an SSH forward. Static product pages do not replace it. Never upload user tokens or account sessions to these pages or GitHub.
