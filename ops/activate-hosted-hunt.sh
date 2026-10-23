#!/usr/bin/env bash
# One-time maintainer activation; end users never run this.
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo on hunt.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bash "$source_dir/deploy-site-hunt.sh"
bash "$source_dir/install-oauth-hunt.sh" /home/valtor/papa-tdl-private/google-web-client.json
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:9013/health
printf '\nHosted sign-in is ready. Run the updated papa-tdl drive-login.\n'
