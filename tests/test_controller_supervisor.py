import errno
import json
from pathlib import Path

import pytest

from deploy.controller_supervisor import heartbeat


@pytest.mark.parametrize("operation", ["write_text", "replace"])
def test_heartbeat_disk_failure_preserves_supervisor_and_old_receipt(tmp_path, monkeypatch, operation):
    receipt = tmp_path / "controller-supervisor.json"
    receipt.write_text('{"time": 1, "controller_pid": 42}')
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")
        patch.setattr(Path, operation, fail)
        assert heartbeat(tmp_path, 43) is False
    assert json.loads(receipt.read_text()) == {"time": 1, "controller_pid": 42}
    assert heartbeat(tmp_path, 43) is True
    updated = json.loads(receipt.read_text())
    assert updated["controller_pid"] == 43
    assert updated["time"] > 1
