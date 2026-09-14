"""Machine firewall bootstrap regression coverage."""

import os
import re
import subprocess
from pathlib import Path

FIREWALL = Path(__file__).resolve().parents[1] / "deploy/firewall-machines.sh"


def test_firewall_script_is_posix_sh_and_parses():
    assert FIREWALL.exists()
    assert os.access(FIREWALL, os.X_OK)
    assert FIREWALL.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    subprocess.run(["sh", "-n", str(FIREWALL)], check=True)


def test_firewall_script_drops_bridge_to_harness_port_in_input():
    """Container-to-host traffic is delivered locally: it goes through
    INPUT, never FORWARD, so a DOCKER-USER rule would not see it."""
    text = FIREWALL.read_text(encoding="utf-8")
    assert 'PORT="${HARNESS_PORT:-8765}"' in text
    assert "-I INPUT 1 -i" in text and '--dport "$PORT" -j DROP' in text
    assert "DOCKER-USER" not in re.sub(r"^#.*$", "", text, flags=re.MULTILINE)
    assert "docker0" in text and "br-*" in text
    assert "--remove" in text
    assert "ip6tables" in text
    # idempotent: check before insert, loop-delete on remove
    assert "-C INPUT" in text


def test_firewall_script_refuses_a_bad_port_before_touching_iptables():
    env = dict(os.environ, HARNESS_PORT="80x")
    proc = subprocess.run(["/bin/sh", str(FIREWALL)], env=env, capture_output=True, text=True)
    assert proc.returncode == 2
    assert "HARNESS_PORT" in proc.stderr


def test_firewall_script_help_does_not_need_root():
    proc = subprocess.run(["/bin/sh", str(FIREWALL), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "--remove" in proc.stdout
