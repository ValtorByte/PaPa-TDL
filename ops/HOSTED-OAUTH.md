# Hosted Google sign-in

The default `papa-tdl drive-login` prints an HTTPS link. Open it on any
computer or phone. After Google approval, the page displays success and the VPS
receives its credentials automatically. No SSH forwarding or user OAuth JSON.

## Maintainer activation (required before publishing this version)

1. Create a **Web application** OAuth client in the existing PaPa-TDL Cloud
   project. Register exactly this authorized redirect URI:
   `https://valtorbyte.ir/papa-tdl-auth/callback`.
2. Store its downloaded JSON privately on hunt, outside the repository, with
   mode 600. Never bundle this Web client secret in a release.
3. Install `flask`, `gunicorn`, `cryptography`, and `requests` in a dedicated
   service virtual environment. Run `oauth_service:app` from this directory with
   Gunicorn bound to `127.0.0.1:9013`, as a dedicated unprivileged account.
4. Provide these environment variables using a mode-600 environment file:
   `PAPA_OAUTH_CLIENT_FILE` (absolute JSON path), `PAPA_OAUTH_DATABASE`
   (absolute SQLite path in a mode-700 private directory), and
   `PAPA_OAUTH_ENCRYPTION_KEY` (generate once with
   `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`).
   Keep the encryption key private and stable across restarts.
5. Proxy `/papa-tdl-auth/` to `http://127.0.0.1:9013/` with Nginx. Set
   `access_log off` for this location, and disable request-body/query logging
   in Cloudflare and any monitoring service. Rate-limit `/start` (5 requests
   per minute per IP), `/refresh` (30 per minute), and `/poll` (30 per minute).
   Keep HTTPS enabled. Do not expose port 9013 publicly.
6. Update the public privacy policy: the sign-in service temporarily handles
   Google codes and tokens, encrypts pending credentials for up to ten minutes,
   and forwards refresh requests to Google. The user's VPS stores its own
   private refresh token. The central service does not retain completed users'
   refresh tokens. Do not claim there is no central authentication service.
7. Test approval, rejection, expiry, service restart and refresh before release.
   Google brand approval and any applicable consent requirements still apply.

SQLite removes expired sessions on each request; run periodic cleanup when the
service is idle. Credentials are deleted on successful polling. A lost response
after delivery requires a new sign-in. Tokens and codes must never enter logs.
The broker is a trusted component: its compromise can expose pending tokens.

The existing personal Desktop flow remains available via:
`papa-tdl drive-login --method browser --client-file /private/desktop.json`.
