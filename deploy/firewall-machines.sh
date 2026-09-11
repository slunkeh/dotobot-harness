#!/bin/sh
# Block bot machines from the harness API.
#
# `harness serve` binds 0.0.0.0 so the Mac/iOS apps can pair over the LAN,
# and every bot machine sits on the docker bridge, from where the host's
# listener is one `curl http://172.17.0.1:8765` away. SECURITY.md requires
# that "bot machines must not be able to reach the harness API"; this is the
# rule that enforces it. Run as root on the harness host, once (idempotent):
#
#   sudo deploy/firewall-machines.sh            # add the drop rules
#   sudo deploy/firewall-machines.sh --remove   # take them out again
#
# Container -> host traffic is delivered locally, so it traverses INPUT, not
# FORWARD: a DOCKER-USER rule would not see it. The rule is inserted at the
# top of INPUT for each bridge interface. Rules do not survive a reboot on
# their own — wire this as ExecStartPre= in harness.service or into your
# iptables persistence (see deploy/README.md).
#
# Tunables (environment):
#   HARNESS_PORT      port the harness API listens on (default 8765)
#   HARNESS_BRIDGES   space-separated bridge interfaces (default: docker0
#                     plus every br-* interface docker created)
set -eu

PORT="${HARNESS_PORT:-8765}"
MODE=add
for arg in "$@"; do
  case "$arg" in
    --remove) MODE=remove ;;
    -h|--help)
      awk 'NR > 1 && /^set -eu/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"
      exit 0
      ;;
    *)
      echo "firewall-machines.sh: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*)
    echo "firewall-machines.sh: HARNESS_PORT must be a number, got '$PORT'" >&2
    exit 2
    ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "firewall-machines.sh must run as root" >&2
  exit 1
fi

bridges="${HARNESS_BRIDGES:-}"
if [ -z "$bridges" ]; then
  bridges="docker0"
  if command -v ip >/dev/null 2>&1; then
    for dev in $(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1); do
      case "$dev" in
        br-*) bridges="$bridges $dev" ;;
      esac
    done
  fi
fi

# One rule per (tool, bridge). `-C` makes add/remove idempotent.
apply() {
  tool="$1"
  dev="$2"
  if ! command -v "$tool" >/dev/null 2>&1; then
    return 0
  fi
  if [ "$MODE" = add ]; then
    if "$tool" -C INPUT -i "$dev" -p tcp --dport "$PORT" -j DROP 2>/dev/null; then
      echo "$tool: $dev -> :$PORT already dropped"
    else
      "$tool" -I INPUT 1 -i "$dev" -p tcp --dport "$PORT" -j DROP
      echo "$tool: dropping $dev -> :$PORT"
    fi
  else
    while "$tool" -C INPUT -i "$dev" -p tcp --dport "$PORT" -j DROP 2>/dev/null; do
      "$tool" -D INPUT -i "$dev" -p tcp --dport "$PORT" -j DROP
      echo "$tool: removed $dev -> :$PORT drop"
    done
  fi
}

if ! command -v iptables >/dev/null 2>&1 && ! command -v ip6tables >/dev/null 2>&1; then
  echo "firewall-machines.sh: neither iptables nor ip6tables found; install iptables (nft backend is fine)" >&2
  exit 1
fi

for dev in $bridges; do
  apply iptables "$dev"
  apply ip6tables "$dev"
done
