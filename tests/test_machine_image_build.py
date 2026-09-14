"""Machine image build script regression coverage."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "deploy/build_machine_image.sh"


def test_build_script_parses_and_help_needs_no_engine():
    subprocess.run(["sh", "-n", str(BUILD)], check=True)
    out = subprocess.run(["sh", str(BUILD), "--help"], capture_output=True, text=True, check=True)
    assert "Dockerfile" in out.stdout or "image" in out.stdout


def test_build_script_skips_cleanly_without_an_engine(tmp_path):
    """Exit 3 is the updater's 'nothing to build' (process backend, laptop)."""
    env = dict(os.environ, HARNESS_CONTAINER_ENGINE=str(tmp_path / "no-such-engine"))
    proc = subprocess.run(["sh", str(BUILD), str(ROOT)], capture_output=True, text=True, env=env)
    assert proc.returncode == 3, proc.stderr
    assert "skipping" in proc.stderr


def test_build_script_builds_from_the_given_root(tmp_path):
    fake = tmp_path / "docker"
    log = tmp_path / "argv.log"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    env = dict(os.environ, HARNESS_CONTAINER_ENGINE=str(fake), HARNESS_MACHINE_IMAGE="hm-test")
    proc = subprocess.run(["sh", str(BUILD), str(ROOT)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "info"
    assert lines[1] == f"build -t hm-test -f {ROOT}/deploy/Dockerfile.machine {ROOT}"
