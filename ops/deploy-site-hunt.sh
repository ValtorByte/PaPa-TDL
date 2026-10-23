#!/usr/bin/env bash
# Publish only the static PaPa-TDL pages under the existing valtorbyte.ir site.
set -Eeuo pipefail
if (( EUID != 0 )); then
  echo 'Run this script with sudo on hunt. It needs to add one Nginx location.' >&2
  exit 1
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../site" && pwd)
include_file=/var/www/valtorbyte.ir/ops/nginx.locations.conf
snippet=/etc/nginx/snippets/papa-tdl-site.conf
base=/var/www/papa-tdl-site
for name in index.html style.css privacy/index.html terms/index.html; do
  test -f "$source_dir/$name" || { echo "Missing site file: $name" >&2; exit 1; }
done
test -f "$include_file"
command -v nginx >/dev/null
nginx -t
if [[ -e "$base/current" && ! -L "$base/current" ]]; then
  echo 'Refusing to replace a non-symlink current directory.' >&2; exit 1
fi
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
backup=/var/backups/papa-tdl-site/$stamp
release=$base/releases/$stamp
install -d -m 755 "$release/privacy" "$release/terms" /etc/nginx/snippets
install -d -m 700 "$backup"
cp -p -- "$include_file" "$backup/nginx.locations.conf"
old_link=$(readlink "$base/current" || true)
snippet_existed=0
if [[ -f "$snippet" ]]; then cp -p -- "$snippet" "$backup/snippet.conf"; snippet_existed=1; fi
rollback() {
  trap - ERR
  echo "Publication failed; restoring previous configuration from $backup" >&2
  cp -p -- "$backup/nginx.locations.conf" "$include_file"
  if (( snippet_existed )); then cp -p -- "$backup/snippet.conf" "$snippet"; else rm -f -- "$snippet"; fi
  rm -f -- "$base/current.next"
  if [[ -n "$old_link" ]]; then
    ln -s -- "$old_link" "$base/current.next"; mv -Tf -- "$base/current.next" "$base/current"
  else rm -f -- "$base/current"; fi
  if nginx -t; then nginx -s reload; fi
  exit 1
}
trap rollback ERR
for name in index.html style.css privacy/index.html terms/index.html; do
  install -m 644 -- "$source_dir/$name" "$release/$name"
done
cat > "$snippet" <<'NGINX'
# PaPa-TDL product pages only; no OAuth callback or file-transfer endpoint.
location = /papa-tdl { return 301 /papa-tdl/; }
location ^~ /papa-tdl/ {
    alias /var/www/papa-tdl-site/current/;
    index index.html;
    autoindex off;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Content-Security-Policy "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" always;
}
NGINX
chmod 644 "$snippet"
if ! grep -Fqx 'include /etc/nginx/snippets/papa-tdl-site.conf;' "$include_file"; then
  printf '\n# Static PaPa-TDL product pages\ninclude /etc/nginx/snippets/papa-tdl-site.conf;\n' >> "$include_file"
fi
ln -s -- "$release" "$base/current.next"
mv -Tf -- "$base/current.next" "$base/current"
nginx -t
nginx -s reload
trap - ERR
printf 'Published: https://valtorbyte.ir/papa-tdl/\nPrivacy: https://valtorbyte.ir/papa-tdl/privacy/\nTerms: https://valtorbyte.ir/papa-tdl/terms/\nBackup: %s\n' "$backup"
