#!/bin/bash
# curl -fsSL https://dotobot.com/install.sh | bash
# Read the whole function before execution so curl's stdin is never reused.
dotobot_install() {
set -euo pipefail
case "${1:-}" in -h|--help)
cat <<'HELP'
Dotobot container installer (Linux or macOS, amd64/arm64)

curl -fsSL https://dotobot.com/install.sh | bash

--ip ADDRESS        public IPv4/IPv6 for HTTPS on ports 80 and 443
--domain NAME       optional DNS name pointing to this server
--port NUMBER       localhost port (default 8765; retain on later commands)
--update            build the latest release, then replace the server
--link              check the installed server and print its private link code
--uninstall         stop/remove Dotobot containers, retaining data and Docker
--delete-data       with --uninstall, also delete all Dotobot data

Without an address, bind only to localhost; no public IP is needed for Mac tests.
Docker is installed if missing. macOS opens Docker Desktop for its license and
permission prompts. Existing Docker installations are reused, never replaced.
DOTOBOT_HOME selects a dedicated installation directory (default ~/.local/share/dotobot).
HARNESS_RELEASE_MANIFEST selects an HTTPS release manifest.
Existing system-service installs retain their original update procedure.
HELP
return ;;
esac
# No prompts or terminal escapes when redirected or used by automation.
interactive=0
[ ! -t 1 ] || [ "${TERM:-dumb}" = dumb ] || interactive=1
accent= reset=
if [ "$interactive" -eq 1 ] && [ -z "${NO_COLOR+x}" ]; then
    accent=$'\033[1;36m'; reset=$'\033[0m'
fi
printf '%s' "$accent"
cat <<'LOGO'
       _       _        _           _
    __| | ___ | |_ ___ | |__   ___ | |_
   / _` |/ _ \| __/ _ \| '_ \ / _ \| __|
  | (_| | (_) | || (_) | |_) | (_) | |_
   \__,_|\___/ \__\___/|_.__/ \___/ \__|

  Your team. Your server.
LOGO
printf '%s\n' "$reset"

dotobot_step() {
    local label=$1 status=0 pid tick=0
    shift
    printf '\n  %s\n' "$label"
    if [ "$interactive" -eq 1 ]; then
        "$@" <&0 >>"$setup_log" 2>&1 &
        pid=$!
        trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; exit 130' INT TERM
        while kill -0 "$pid" 2>/dev/null; do
            printf '\r  [%*s====%*s] Working...' "$((tick % 16))" '' "$((16 - tick % 16))" ''
            tick=$((tick + 1))
            sleep 0.2
        done
        wait "$pid" || status=$?
        trap - INT TERM
        printf '\r%60s\r' ''
    else
        "$@" >>"$setup_log" 2>&1 || status=$?
    fi
    if [ "$status" -ne 0 ]; then
        printf '\n  Setup stopped. Your diagnostic log: %s\n' "$setup_log" >&2
        tail -n 20 "$setup_log" >&2
    fi
    return "$status"
}

printf '  [--------------------] 0/5  Checking Docker\n'
case "$(uname -s)" in Linux|Darwin) ;; *) echo 'Use Linux, macOS, or a Linux shell with Docker integration.' >&2; return 1;; esac
if [ "$(uname -s)" = Darwin ] && [ "$(id -u)" -eq 0 ]; then
    echo 'Run this installer as your normal Mac user; it requests administrator permission only when needed.' >&2
    return 1
fi
owner_home=$HOME
if [ -n "${SUDO_USER:-}" ] && command -v getent >/dev/null 2>&1; then
    found_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    [ -z "$found_home" ] || owner_home=$found_home
fi
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo '64-bit amd64 or arm64 is required.' >&2; return 1;; esac
# Preserve the existing system install contract, including its timer and data.
if [ -d /etc/dotobot-server ] && [ -f /opt/harness/current/deploy/public_install.py ]; then
    if [ "$(id -u)" -eq 0 ]; then
        exec /usr/bin/python3 /opt/harness/current/deploy/public_install.py "$@"
    fi
    exec sudo /usr/bin/python3 /opt/harness/current/deploy/public_install.py "$@"
fi
for legacy in "${HARNESS_INSTALL_DIR:-}" "$owner_home/.dotobot" "$owner_home/.agent-harness" "$owner_home/agent-harness" /opt/harness/current; do
    if [ -n "$legacy" ] && [ -f "$legacy/harness/__init__.py" ]; then
        echo "Existing source installation at $legacy was left unchanged. Use its current update procedure." >&2
        return 1
    fi
done
if [ -e /etc/systemd/system/harness.service ]; then
    echo 'An existing harness system service was left unchanged.' >&2; return 1
fi
# Only install Docker when neither its CLI nor the existing Mac app is present.
if [ "$(uname -s)" = Darwin ]; then
    export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi
if ! command -v docker >/dev/null 2>&1; then
    echo 'Docker is required. Installing Docker using its official installer.'
    docker_stage=$(mktemp -d)
    if [ "$(uname -s)" = Darwin ]; then
        arch=arm64; [ "$(uname -m)" != x86_64 ] || arch=amd64
        curl -fL --proto '=https' --tlsv1.2 "https://desktop.docker.com/mac/main/$arch/Docker.dmg" -o "$docker_stage/Docker.dmg"
        mkdir "$docker_stage/mount"
        hdiutil attach "$docker_stage/Docker.dmg" -nobrowse -mountpoint "$docker_stage/mount"
        if ! spctl --assess --type execute "$docker_stage/mount/Docker.app"; then
            hdiutil detach "$docker_stage/mount"; return 1
        fi
        if ! sudo "$docker_stage/mount/Docker.app/Contents/MacOS/install"; then
            hdiutil detach "$docker_stage/mount"; return 1
        fi
        hdiutil detach "$docker_stage/mount"
    else
        curl -fsSL --proto '=https' https://get.docker.com -o "$docker_stage/install-docker.sh"
        if [ "$(id -u)" -eq 0 ]; then sh "$docker_stage/install-docker.sh"; else sudo sh "$docker_stage/install-docker.sh"; fi
    fi
    rm -rf "$docker_stage"
fi
docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then
    if [ "$(uname -s)" = Darwin ]; then
        echo 'Starting Docker Desktop. Complete its license and permission prompts if shown.'
        open -a Docker
        for attempt in {1..120}; do
            docker info >/dev/null 2>&1 && break
            sleep 2
        done
    elif sudo docker info >/dev/null 2>&1; then
        docker_cmd=(sudo docker)
    else
        echo 'Docker is installed but unavailable. Start your Docker engine and rerun this command.' >&2
        return 1
    fi
fi
"${docker_cmd[@]}" info >/dev/null
[ "$("${docker_cmd[@]}" info --format '{{.OSType}}')" = linux ] || { echo 'Docker must run Linux containers.' >&2; return 1; }
# Bind mounts refer to the engine host, so reject remote contexts rather than
# accidentally writing to a remote host with misleading local paths.
endpoint=$("${docker_cmd[@]}" context inspect --format '{{.Endpoints.docker.Host}}')
endpoint="${DOCKER_HOST:-$endpoint}"
case "$endpoint" in unix://*) ;; *) echo 'Use a local Docker context; remote engines need an explicit deployment on that host.' >&2; return 1;; esac
socket=${endpoint#unix://}
if [ "$(uname -s)" = Darwin ]; then socket=/var/run/docker.sock; fi
export DOTOBOT_DOCKER_SOCKET="$socket"
root=${DOTOBOT_HOME:-$owner_home/.local/share/dotobot}
case "$root" in /*) ;; *) echo 'DOTOBOT_HOME must be an absolute dedicated directory.' >&2; return 1;; esac
case "$root" in /|"$HOME"|/root|/home|/Users|*','*|*':'*) echo 'Use a dedicated Dotobot directory without commas or colons.' >&2; return 1;; esac
mkdir -p "$root"
root=$(cd "$root" && pwd -P)
chmod 700 "$root"
maintenance=0
for arg in "$@"; do case "$arg" in --link|--uninstall) maintenance=1;; esac; done
if [ "$maintenance" -eq 1 ]; then
    [ -f "$root/manager-image" ] || { echo 'No container installation found.' >&2; return 1; }
    image=$(cat "$root/manager-image")
    [[ "$image" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'Invalid installed image record.' >&2; return 1; }
    "${docker_cmd[@]}" run --rm --mount "type=bind,src=$root,dst=$root" --mount "type=bind,src=$socket,dst=/var/run/docker.sock" "$image" python deploy/container_install.py --root "$root" "$@"
    return
fi
setup_log="$root/setup.log"
(umask 077; : > "$setup_log")
chmod 600 "$setup_log"
stage=$(mktemp -d "$root/download.XXXXXXXX")
# The release is downloaded and verified inside Python's container: the host
# needs Docker, curl and Bash, not a particular Python or Linux distribution.
manifest=${HARNESS_RELEASE_MANIFEST:-https://releases.dotobot.com/harness/manifest.json}
dotobot_step "[####----------------] 1/5  Downloading and verifying the release" "${docker_cmd[@]}" run --rm -i --user "$(id -u):$(id -g)" --mount "type=bind,src=$stage,dst=$stage" python:3.12-slim python - "$manifest" "$stage" <<'PY'
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request

url, stage = sys.argv[1], Path(sys.argv[2])
def origin(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment:
        raise SystemExit('Release URLs must use HTTPS without credentials.')
    return p.scheme, p.netloc.lower()

class SameOrigin(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(newurl) != origin(url):
            raise SystemExit('Release redirect left its trusted origin.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

opener = urllib.request.build_opener(SameOrigin())
origin(url)
with opener.open(url, timeout=30) as response:
    raw = response.read(1048577)
if len(raw) > 1048576:
    raise SystemExit('Release manifest is too large.')
manifest = json.loads(raw)
if not re.fullmatch(r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}', str(manifest.get('version', ''))):
    raise SystemExit('Invalid release version.')
if not re.fullmatch(r'[a-fA-F0-9]{64}', str(manifest.get('sha256', ''))):
    raise SystemExit('Invalid release checksum.')
if origin(manifest['url']) != origin(url):
    raise SystemExit('Release archive must use the manifest origin.')
archive = stage / 'release.tar.gz'
with opener.open(manifest['url'], timeout=300) as response, archive.open('wb') as out:
    total, digest = 0, hashlib.sha256()
    while chunk := response.read(1048576):
        total += len(chunk)
        if total > 128 * 1048576:
            raise SystemExit('Release archive is too large.')
        digest.update(chunk)
        out.write(chunk)
if digest.hexdigest() != manifest['sha256'].lower():
    raise SystemExit('Release checksum mismatch; nothing was installed.')
release = stage / 'release'
release.mkdir()
with tarfile.open(archive) as tar:
    members = tar.getmembers()
    if len(members) > 10000 or sum(m.size for m in members) > 512 * 1048576:
        raise SystemExit('Release archive exceeds its unpacking limit.')
    for member in members:
        p = PurePosixPath(member.name)
        if p.is_absolute() or '..' in p.parts or not (member.isdir() or member.isfile()):
            raise SystemExit('Unsafe release archive member.')
    # Debian 12's Python 3.11 has no tarfile extraction-filter argument.
    # Explicit regular-file copies never apply archive ownership or links.
    for member in members:
        target = release / member.name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o755)
(stage / 'manifest.json').write_text(json.dumps(manifest))
if not (release / 'deploy/container_install.py').is_file():
    raise SystemExit('This release predates the container installer. Try again after the self-hosted release is published.')
PY
dotobot_step "[########------------] 2/5  Building your server" "${docker_cmd[@]}" build --iidfile "$stage/server-image" -f "$stage/release/deploy/Dockerfile" "$stage/release"
image=$(cat "$stage/server-image")
"${docker_cmd[@]}" run --rm --mount "type=bind,src=$root,dst=$root" --mount "type=bind,src=$socket,dst=/var/run/docker.sock" -e "DOTOBOT_DOCKER_SOCKET=$socket" -e "DOTOBOT_SETUP_LOG=$setup_log" -e "DOTOBOT_INSTALL_TTY=$interactive" "$image" python deploy/container_install.py --root "$root" --source "$stage/release" --image "$image" "$@"
rm -rf "$stage"
}
dotobot_install "$@"
