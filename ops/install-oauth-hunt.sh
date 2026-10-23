#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo on hunt.' >&2; exit 1; }
client=${1:?Usage: sudo bash install-oauth-hunt.sh /private/web-client.json}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 - "$client" <<'PY'
import json, sys
from pathlib import Path
v = json.loads(Path(sys.argv[1]).read_text())['web']
assert v['client_id'] and v['client_secret']
assert 'https://valtorbyte.ir/papa-tdl-auth/callback' in v['redirect_uris']
PY
[[ -d /run/systemd/system ]] || { echo 'This hunt deployment requires systemd.' >&2; exit 1; }
nginx -t
id papa-tdl-auth >/dev/null 2>&1 || useradd --system --home /var/lib/papa-tdl-auth --shell /usr/sbin/nologin papa-tdl-auth
install -d -m 755 /opt/papa-tdl-auth
install -d -m 700 -o papa-tdl-auth -g papa-tdl-auth /var/lib/papa-tdl-auth
install -m 600 -o papa-tdl-auth -g papa-tdl-auth "$client" /var/lib/papa-tdl-auth/client.json
python3 -m venv /opt/papa-tdl-auth/venv
/opt/papa-tdl-auth/venv/bin/pip install 'flask>=3.1,<4' 'gunicorn>=23,<24' 'cryptography>=44' 'requests>=2.32,<3'
install -m 644 "$source_dir/oauth_service.py" /opt/papa-tdl-auth/oauth_service.py
if [[ ! -f /var/lib/papa-tdl-auth/service.env ]]; then
    umask 077
    /opt/papa-tdl-auth/venv/bin/python - <<'PY' > /var/lib/papa-tdl-auth/service.env
from cryptography.fernet import Fernet
print('PAPA_OAUTH_CLIENT_FILE=/var/lib/papa-tdl-auth/client.json')
print('PAPA_OAUTH_DATABASE=/var/lib/papa-tdl-auth/sessions.sqlite')
print('PAPA_OAUTH_ENCRYPTION_KEY=' + Fernet.generate_key().decode())
PY
fi
install -m 644 "$source_dir/papa-tdl-auth.service" /etc/systemd/system/papa-tdl-auth.service
install -m 644 "$source_dir/papa-tdl-auth.conf" /etc/nginx/conf.d/papa-tdl-auth.conf
include=/var/www/valtorbyte.ir/ops/nginx.locations.conf
backup=$(mktemp /var/backups/papa-tdl-auth-locations.XXXXXXXX)
cp -p "$include" "$backup"
if ! grep -Fq 'location ^~ /papa-tdl-auth/' "$include"; then
    printf '\nlocation ^~ /papa-tdl-auth/ {\n access_log off;\n limit_req zone=papa_tdl_auth burst=20 nodelay;\n proxy_pass http://127.0.0.1:9013/;\n proxy_set_header Host $host;\n proxy_set_header X-Forwarded-Proto https;\n proxy_read_timeout 30s;\n client_max_body_size 16k;\n}\n' >> "$include"
fi
if ! nginx -t; then cp -p "$backup" "$include"; exit 1; fi
systemctl daemon-reload
systemctl enable --now papa-tdl-auth
systemctl restart papa-tdl-auth
nginx -s reload
echo 'PaPa-TDL authentication service started. Test a real login before releasing.'
