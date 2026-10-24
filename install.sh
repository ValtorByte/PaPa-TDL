#!/usr/bin/env bash
# PaPa-TDL native installer; supports local checkout and bash -c remote bootstrap.
set -euo pipefail
umask 022
usage() {
  cat <<'TXT'
PaPa-TDL installer
  install.sh install [--skip-system-deps]
  install.sh update  [--skip-system-deps]
  install.sh help

Environment: PAPA_TDL_REPO, PAPA_TDL_REF, PAPA_TDL_PREFIX,
PAPA_TDL_BIN_DIR, PAPA_TDL_DATA_DIR, PAPA_TDL_USER, PAPA_TDL_PYTHON,
PAPA_TDL_BOOTSTRAP_PYTHON (an interpreter with pip 22.3+).
No systemd or Docker daemon is required. Reinstall/update preserves data.
TXT
}
command_name=${1:-install}
case "$command_name" in
  install|update) if (( $# )); then shift; fi ;;
  help|--help|-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
skip_deps=0
for argument in "$@"; do
  case "$argument" in
    --skip-system-deps) skip_deps=1 ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done
[[ $(uname -s) == Linux ]] || { echo 'PaPa-TDL targets Linux hosts.' >&2; exit 1; }
repo=${PAPA_TDL_REPO:-ValtorByte/PaPa-TDL}
ref=${PAPA_TDL_REF:-main}
[[ "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo 'Invalid repository.' >&2; exit 1; }
[[ "$ref" =~ ^[A-Za-z0-9_./-]+$ && "$ref" != -* && "$ref" != *..* ]] || { echo 'Invalid Git ref.' >&2; exit 1; }
printf '\n\033[95m  PaPa-TDL / INSTALLER 1.6.5\033[0m\n  Telegram files → Drive or local storage\n\n'
echo '[1/5] Preparing the host...'
root=0
[[ $(id -u) == 0 ]] && root=1
if (( root )); then
  prefix=${PAPA_TDL_PREFIX:-/opt/papa-tdl}
  bin_dir=${PAPA_TDL_BIN_DIR:-/usr/local/bin}
  run_as=${PAPA_TDL_USER:-${SUDO_USER:-papa-tdl}}
  if [[ "$run_as" == root && -z ${PAPA_TDL_USER:-} ]]; then run_as=papa-tdl; fi
  [[ "$run_as" != root ]] || { echo 'Use a non-root PAPA_TDL_USER.' >&2; exit 1; }
  if ! id "$run_as" >/dev/null 2>&1; then
    [[ "$run_as" == papa-tdl ]] || { echo 'PAPA_TDL_USER must already exist.' >&2; exit 1; }
    useradd --system --create-home --home-dir /var/lib/papa-tdl --shell /bin/bash papa-tdl
  fi
  account_home=$(getent passwd "$run_as" | cut -d: -f6)
  data_dir=${PAPA_TDL_DATA_DIR:-"$account_home/.local/share/papa-tdl"}
  command -v runuser >/dev/null || { echo 'runuser is required (util-linux package).' >&2; exit 1; }
else
  prefix=${PAPA_TDL_PREFIX:-"$HOME/.local/share/papa-tdl/program"}
  bin_dir=${PAPA_TDL_BIN_DIR:-"$HOME/.local/bin"}
  data_dir=${PAPA_TDL_DATA_DIR:-"$HOME/.local/share/papa-tdl"}
  run_as=$(id -un)
fi
for path in "$prefix" "$bin_dir" "$data_dir"; do
  [[ "$path" == /* && "$path" != / && "$path" != *$'\n'* ]] || { echo 'Installation paths must be absolute dedicated directories.' >&2; exit 1; }
done
if (( ! skip_deps )); then
  command -v apt-get >/dev/null || { echo 'Use Debian 12+/Ubuntu 24.04+, or prepare dependencies and pass --skip-system-deps.' >&2; exit 1; }
  privilege=()
  if (( ! root )); then privilege=(sudo); fi
  "${privilege[@]}" apt-get update
  archive_package=p7zip-full
  apt-cache show 7zip >/dev/null 2>&1 && archive_package=7zip
  "${privilege[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip ca-certificates curl "$archive_package"
  if apt-cache show unrar >/dev/null 2>&1; then
    "${privilege[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y unrar
  elif apt-cache show 7zip-rar >/dev/null 2>&1; then
    "${privilege[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y 7zip-rar
  else
    echo 'RAR support is unavailable from configured package sources; enable the multiverse/non-free repository for RAR extraction.' >&2
  fi
fi
# Use the interpreter matching the apt packages, not a /usr/local or conda shim.
if [[ -n ${PAPA_TDL_PYTHON:-} ]]; then
  python=$PAPA_TDL_PYTHON
elif [[ -x /usr/bin/python3 ]]; then
  python=/usr/bin/python3
else
  python=python3
fi
bootstrap=${PAPA_TDL_BOOTSTRAP_PYTHON:-$python}
if ! "$bootstrap" -m pip --version; then
  echo 'A working pip 22.3+ is required to bootstrap the private environment.' >&2
  echo 'On Ubuntu/Debian: sudo apt-get install python3-pip python3-venv' >&2
  echo 'Or set PAPA_TDL_BOOTSTRAP_PYTHON to an interpreter with pip.' >&2
  exit 1
fi
"$python" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required; use Debian 12+ or Ubuntu 24.04+"'
mkdir -p "$prefix/releases" "$bin_dir"
if (( root )); then
  install -d -m 700 -o "$run_as" -g "$(id -gn "$run_as")" "$data_dir"
else
  install -d -m 700 "$data_dir"
fi
# Hold the profile lock throughout the update, including activation.
command -v flock >/dev/null || { echo 'flock is required (util-linux package).' >&2; exit 1; }
exec {install_lock}>"$data_dir/app.lock"
if ! flock -n "$install_lock"; then
  echo 'Stop the watcher before updating: papa-tdl stop' >&2
  exit 1
fi
if (( root )); then chown "$run_as:$(id -gn "$run_as")" "$data_dir/app.lock"; fi
stage=$(mktemp -d "$prefix/releases/1.6.5-XXXXXXXX")
chmod 755 "$stage"
temporary_source=''
completed=0
cleanup() {
  "$python" - "$temporary_source" "$stage" "$completed" <<'PY'
import shutil, sys
if sys.argv[1]:
    shutil.rmtree(sys.argv[1], ignore_errors=True)
if sys.argv[3] != '1':
    shutil.rmtree(sys.argv[2], ignore_errors=True)
PY
}
trap cleanup EXIT
source_dir=''
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then
  candidate=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  if [[ -f "$candidate/pyproject.toml" && -d "$candidate/src/tdl_watcher" ]]; then source_dir=$candidate; fi
fi
if [[ -z "$source_dir" ]]; then
  temporary_source=$(mktemp -d)
  echo "Downloading $repo ($ref)..."
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
    "https://api.github.com/repos/$repo/tarball/$ref" -o "$temporary_source/source.tar.gz"
  mkdir "$temporary_source/source"
  tar -xzf "$temporary_source/source.tar.gz" --strip-components=1 --no-same-owner -C "$temporary_source/source"
  source_dir=$temporary_source/source
fi
mkdir "$stage/app"
for item in pyproject.toml requirements.lock README.md LICENSE src; do
  cp -R -- "$source_dir/$item" "$stage/app/"
done
echo '[2/5] Creating the private Python environment (without ensurepip)...'
"$python" -m venv --without-pip "$stage/venv"
# pip's --python option manages a venv that does not contain pip yet.
env -u PYTHONPATH -u PYTHONHOME -u PIP_TARGET -u PIP_PREFIX -u PIP_USER \
  "$bootstrap" -m pip --python "$stage/venv/bin/python" install --upgrade pip
"$stage/venv/bin/python" -m pip --version
echo '[3/5] Installing PaPa-TDL packages...'
env -u PYTHONPATH -u PYTHONHOME -u PIP_TARGET -u PIP_PREFIX -u PIP_USER \
  "$stage/venv/bin/python" -m pip install -r "$stage/app/requirements.lock"
env -u PYTHONPATH -u PYTHONHOME -u PIP_TARGET -u PIP_PREFIX -u PIP_USER \
  "$stage/venv/bin/python" -m pip install --no-deps "$stage/app"
echo '[4/5] Installing Chromium...'
export PLAYWRIGHT_BROWSERS_PATH="$stage/browsers"
find_system_browser() {
  local candidate
  for candidate in google-chrome-stable google-chrome chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}
system_browser=$(find_system_browser || true)
if [[ -n "$system_browser" ]]; then
  echo "Using system browser: $system_browser"
else
  playwright_ok=0
  if (( skip_deps )); then
    "$stage/venv/bin/python" -m playwright install chromium && playwright_ok=1
  elif (( root )); then
    "$stage/venv/bin/python" -m playwright install --with-deps chromium && playwright_ok=1
  else
    "$stage/venv/bin/python" -m playwright install-deps chromium
    "$stage/venv/bin/python" -m playwright install chromium && playwright_ok=1
  fi
  if (( ! playwright_ok )); then
    echo 'Playwright CDN is unavailable; trying the official system Chrome package...'
    if [[ $(uname -m) == x86_64 && $skip_deps == 0 ]]; then
      chrome_deb="$stage/google-chrome-stable_current_amd64.deb"
      if curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
          https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
          -o "$chrome_deb"; then
        "${privilege[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y "$chrome_deb" || true
      fi
      system_browser=$(find_system_browser || true)
    fi
    if [[ -z "$system_browser" ]]; then
      echo 'ERROR: No Chromium browser could be installed.' >&2
      echo 'The Playwright CDN and the Chrome fallback are unavailable from this server.' >&2
      echo 'Install chromium/google-chrome, then run this installer again.' >&2
      exit 1
    fi
    echo "Using system browser: $system_browser"
  fi
fi
echo '[5/5] Activating the launcher...'
chmod -R a+rX "$stage"
launcher="$stage/papa-tdl"
{
  echo '#!/usr/bin/env bash'
  echo 'set -euo pipefail'
  printf 'runtime_user=%q\n' "$run_as"
  printf 'program=%q\n' "$prefix/current"
  printf 'default_data=%q\n' "$data_dir"
  printf 'system_browser=%q\n' "$system_browser"
  cat <<'WRAPPER'
export TDL_DATA_DIR=${TDL_DATA_DIR:-"$default_data"}
export PLAYWRIGHT_BROWSERS_PATH="$program/browsers"
if [[ -n "$system_browser" ]]; then
  export TDL_CHROMIUM_EXECUTABLE="$system_browser"
fi
if [[ $(id -u) == 0 && "$runtime_user" != root ]]; then
  exec runuser -u "$runtime_user" -- env TDL_DATA_DIR="$TDL_DATA_DIR" \
    PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" \
    TDL_CHROMIUM_EXECUTABLE="${TDL_CHROMIUM_EXECUTABLE:-}" \
    "$program/venv/bin/python" -m tdl_watcher "$@"
fi
if [[ $(id -un) != "$runtime_user" ]]; then
  echo "This installation belongs to $runtime_user. Run it as that user." >&2
  exit 1
fi
exec "$program/venv/bin/python" -m tdl_watcher "$@"
WRAPPER
} > "$launcher"
chmod 755 "$launcher"
install -m 755 "$launcher" "$bin_dir/papa-tdl"
ln -sfn "$stage" "$prefix/.current-new"
mv -Tf "$prefix/.current-new" "$prefix/current"
completed=1
printf '\n\033[95mPaPa-TDL installed.\033[0m\nLauncher: %s/papa-tdl\nPrivate data: %s\nRuntime user: %s\n' "$bin_dir" "$data_dir" "$run_as"
printf '\nLaunch: papa-tdl\nFirst launch opens the setup wizard. Default destination: Google Drive / TDL.\n'
printf 'Use local storage if you prefer. No systemd or Docker is required.\n'
