#!/usr/bin/env bash
# Serve a Google-provided public ownership proof; Google verification is separate.
set -Eeuo pipefail
if (( EUID != 0 )); then echo 'Run with sudo on hunt.' >&2; exit 1; fi
proof=${1:?Usage: sudo bash verify-google-domain-hunt.sh /path/to/google-proof.html}
name=$(basename -- "$proof")
[[ "$name" =~ ^google[a-f0-9]+\.html$ ]] || { echo 'Unexpected verification filename.' >&2; exit 1; }
python3 - "$proof" "$name" <<'PY'
import sys
from pathlib import Path
assert Path(sys.argv[1]).read_text().strip() == 'google-site-verification: ' + sys.argv[2], 'Unexpected ownership proof content'
PY
include_file=/var/www/valtorbyte.ir/ops/nginx.locations.conf
snippet=/etc/nginx/snippets/papa-tdl-google-verification.conf
test -f "$include_file"
nginx -t
backup=$(mktemp -d /var/backups/papa-tdl-verification.XXXXXXXX)
cp -p -- "$include_file" "$backup/locations.conf"
existed=0
if [[ -f "$snippet" ]]; then cp -p -- "$snippet" "$backup/proof.conf"; existed=1; fi
restore() {
  cp -p -- "$backup/locations.conf" "$include_file"
  if (( existed )); then cp -p -- "$backup/proof.conf" "$snippet"; else rm -f -- "$snippet"; fi
}
install -d -m 755 /var/www/papa-tdl-site/verification
install -m 644 -- "$proof" "/var/www/papa-tdl-site/verification/$name"
if ! grep -Fqx "location = /$name {" "$snippet"; then
  cat >> "$snippet" <<NGINX

# Public ownership proof for the maintainer's Google Search Console account.
location = /$name {
    alias /var/www/papa-tdl-site/verification/$name;
    default_type text/html;
    add_header X-Content-Type-Options nosniff always;
}
NGINX
fi
if ! grep -Fqx 'include /etc/nginx/snippets/papa-tdl-google-verification.conf;' "$include_file"; then
  printf '\ninclude /etc/nginx/snippets/papa-tdl-google-verification.conf;\n' >> "$include_file"
fi
if ! nginx -t; then restore; echo 'Config check failed; previous config restored.' >&2; exit 1; fi
if ! nginx -s reload; then restore; nginx -t && nginx -s reload; exit 1; fi
printf 'Ownership proof published: https://valtorbyte.ir/%s\nBackup: %s\n' "$name" "$backup"
