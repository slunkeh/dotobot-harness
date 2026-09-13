#!/bin/bash
# Install a self-hosted Dotobot server. The apps are sold separately.
# curl -fsSL https://dotobot.com/install.sh | bash
# Load the complete installer before running it: stdin may be a curl pipe.
dotobot_install() {
set -eu

case "${1:-}" in
    -h|--help)
        cat <<'HELP'
Dotobot server installer (Ubuntu 24.04 / Debian 12, amd64 or arm64)

curl -fsSL https://dotobot.com/install.sh | bash

--ip ADDRESS        public IPv4 or IPv6 address; no domain needed
--domain NAME       optional DNS name pointing to this server
--update            install the latest release on an existing self-hosted install
--auto-update       opt in to automatic daily updates
--no-auto-update    turn automatic updates off (the initial default)
--link              print the installed server's link code after checking HTTPS

When neither address option is supplied, enter a public IP or domain at the prompt.
Requests administrator permission with sudo when needed.
Requires inbound TCP 80/443 for Caddy's HTTPS certificate. IP certificates
renew automatically; private/LAN addresses cannot receive public IP certificates.
No Dotobot account is needed on the server. Existing installations retain their
state, link key, address and update policy.
HARNESS_RELEASE_MANIFEST overrides the HTTPS release manifest URL.
HELP
        exit 0 ;;
esac

# Source checkouts use their original update path; never create a second home.
LEGACY_HOME="${HOME:-/root}"
if [ -n "${SUDO_USER:-}" ] && command -v getent >/dev/null 2>&1; then
    OWNER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    [ -z "$OWNER_HOME" ] || LEGACY_HOME="$OWNER_HOME"
fi
for LEGACY_DIR in "${HARNESS_INSTALL_DIR:-}" "$LEGACY_HOME/.dotobot" "$LEGACY_HOME/.agent-harness"; do
    if [ -n "$LEGACY_DIR" ] && [ -f "$LEGACY_DIR/harness/__init__.py" ]; then
        echo "Existing source installation at $LEGACY_DIR was left unchanged. Update that checkout using its existing procedure; this installer creates new system services only." >&2
        exit 1
    fi
done

[ "$(uname -s)" = Linux ] || { echo 'The server installer supports Linux; install the apps from the App Store.' >&2; exit 1; }
if [ "$(id -u)" -ne 0 ]; then
    [ -n "${BASH_VERSION:-}" ] || {
        echo 'Run: curl -fsSL https://dotobot.com/install.sh | bash' >&2
        exit 1
    }
    command -v sudo >/dev/null 2>&1 || {
        echo 'Administrator permission is required. Install sudo or run this installer as root.' >&2
        exit 1
    }
    echo 'Dotobot needs administrator permission to install packages and system services.' >&2
    # Serialize the loaded function instead of re-reading an exhausted pipe or
    # downloading a second copy. Pass settings and options as literal arguments.
    exec sudo /bin/bash -c "$(declare -f dotobot_install)
export HARNESS_RELEASE_MANIFEST=\"\$1\" HARNESS_INSTALL_DIR=\"\$2\"
shift 2
dotobot_install \"\$@\"" dotobot-install "${HARNESS_RELEASE_MANIFEST:-}" "${HARNESS_INSTALL_DIR:-}" "$@"
fi
# Never adopt a managed/personal server merely because it uses the same paths.
if [ -f /etc/dotobot-server/install.json ] && [ -f /opt/harness/current/deploy/public_install.py ]; then
    exec /usr/bin/python3 /opt/harness/current/deploy/public_install.py "$@"
fi
if [ ! -f /etc/dotobot-server/install.json ] && { [ -e /opt/harness/current ] || [ -e /etc/systemd/system/harness.service ]; }; then
    echo 'An existing harness installation was found. It has been left unchanged; use its existing update procedure.' >&2
    exit 1
fi
# shellcheck source=/dev/null
. /etc/os-release
case "$ID:$VERSION_ID" in
    ubuntu:24.04|debian:12) ;;
    *) echo 'Supported server systems: Ubuntu 24.04, Debian 12.' >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64|aarch64|arm64) ;;
    *) echo 'Supported server architectures: amd64 and arm64 (64-bit).' >&2; exit 1 ;;
esac

if ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends python3 curl ca-certificates
fi
MANIFEST="${HARNESS_RELEASE_MANIFEST:-https://releases.dotobot.com/harness/manifest.json}"
STAGE=$(mktemp -d /tmp/dotobot-install.XXXXXXXX)
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM
# Verify the archive before executing any code inside it. The bootstrap is
# intentionally self-contained: neither git nor private repository access is used.
python3 - "$MANIFEST" "$STAGE" <<'PY'
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
if not (release / 'deploy/public_install.py').is_file():
    raise SystemExit('This release predates the public installer. Try again after the self-hosted release is published.')
PY
python3 "$STAGE/release/deploy/public_install.py" --release-tree "$STAGE/release" --release-manifest "$STAGE/manifest.json" --manifest-url "$MANIFEST" "$@"

}

dotobot_install "$@"
