"""Two real services keep same-named bots, state and bearer keys independent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from harness.orchestrator import Orchestrator
from tests.test_ws import WSClient

ROOT = Path(__file__).resolve().parents[1]


def request(server, path, method="GET", data=None, *, key=None, headers=None):
    url, token, _ = server
    fields = {"Authorization": "Bearer " + (key if key is not None else token)}
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        fields["Content-Type"] = "application/json"
    fields.update(headers or {})
    req = urllib.request.Request(url + path, data=data, headers=fields, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        content = response.read()
        return (
            json.loads(content) if "json" in response.headers.get("Content-Type", "") else content
        )


def until(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    pytest.fail("local harness did not become ready")


@contextmanager
def service(directory):
    directory.mkdir()
    home = directory / "home"
    roster = directory / "roster.toml"
    roster.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n')
    token = uuid.uuid4().hex
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HARNESS_TOKEN": token,
        "HARNESS_AUTO_ROLL": "0",
    }
    # actions/setup-python's shared-library build needs its loader search path.
    # Keep the rest of the service environment isolated from host credentials.
    if "LD_LIBRARY_PATH" in os.environ:
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    command = [
        sys.executable,
        "-c",
        "import sys; from harness.server import serve; "
        "serve(home=sys.argv[1], roster_path=sys.argv[2], backend='process', "
        "host='127.0.0.1', port=0, start_bots=True)",
        str(home),
        str(roster),
    ]
    with (directory / "service.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            until(lambda: (home / "serve.json").exists() or process.poll() is not None)
            assert process.poll() is None, (directory / "service.log").read_text()
            info = json.loads((home / "serve.json").read_text())
            server = (info["url"], token, home)
            until(lambda: request(server, "/api/bots")[0]["status"] == "running")
            yield server
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            finally:
                # Serve deliberately preserves agents on shutdown; stop only this home.
                Orchestrator.create(home=home, roster_path=roster, backend="process").down()


def test_two_linked_services_keep_chat_uploads_restart_and_keys_independent(tmp_path):
    with service(tmp_path / "first") as first, service(tmp_path / "second") as second:
        clients = []
        try:
            for server, other, marker in (
                (first, second, "alpha-only"),
                (second, first, "beta-only"),
            ):
                assert [row["name"] for row in request(server, "/api/bots")] == ["atlas"]
                for path, method in (
                    ("/api/bots", "GET"),
                    ("/ws", "GET"),
                    ("/api/bots/atlas/restart", "POST"),
                    ("/api/upload", "POST"),
                ):
                    with pytest.raises(urllib.error.HTTPError) as refused:
                        request(server, path, method, key=other[1])
                    assert refused.value.code == 401
                upload = request(
                    server,
                    "/api/upload",
                    "POST",
                    marker.encode(),
                    headers={"X-Filename": "brief.txt"},
                )
                path = "/api/uploads/" + Path(upload["path"]).name
                assert request(server, path) == marker.encode()
                with pytest.raises(urllib.error.HTTPError) as missing:
                    request(other, path)
                assert missing.value.code == 404
                port = int(server[0].rsplit(":", 1)[1])
                client = WSClient("127.0.0.1", port, "/ws?token=" + server[1])
                clients.append(client)
                client.send(
                    {"type": "chat", "bot": "atlas", "text": marker, "attachments": [upload]}
                )
            for client, marker, absent in zip(
                clients, ("alpha-only", "beta-only"), ("beta-only", "alpha-only"), strict=True
            ):
                frames = client.recv_until("final")
                final = frames[-1]
                assert final["type"] == "final" and marker in final["text"]
                assert "brief.txt" in final["text"] and absent not in json.dumps(frames)
            for server, marker, absent in (
                (first, "alpha-only", "beta-only"),
                (second, "beta-only", "alpha-only"),
            ):
                history = json.dumps(request(server, "/api/bots/atlas/history"))
                assert marker in history and absent not in history
            before = [
                json.loads((server[2] / "run/atlas.json").read_text())["pid"]
                for server in (first, second)
            ]
            request(first, "/api/bots/atlas/restart", "POST")
            until(
                lambda: (
                    request(first, "/api/bots")[0]["status"] == "running"
                    and json.loads((first[2] / "run/atlas.json").read_text())["pid"] != before[0]
                )
            )
            assert json.loads((second[2] / "run/atlas.json").read_text())["pid"] == before[1]
            clients[0].send({"type": "chat", "bot": "atlas", "text": "after-restart"})
            assert "after-restart" in clients[0].recv_until("final")[-1]["text"]
            assert "alpha-only" in json.dumps(request(first, "/api/bots/atlas/history"))
            assert "after-restart" not in json.dumps(request(second, "/api/bots/atlas/history"))
        finally:
            for client in clients:
                client.rfile.close()
                client.close()
