"""Install the request worker without changing automatic release policy."""

from __future__ import annotations

import json


def install(layout, config, *, write, runner):
    # Linking is read-only; an installed request worker needs no reinstall.
    if (layout.config_dir / "update-host.json").exists():
        return
    worker_config = {
        "home": str(layout.home),
        "root": str(layout.root),
        "manifest_url": config["manifest_url"],
        "service": "dotobot-server.service",
        "image": "dotobot-server-machine",
    }
    config_path = layout.config_dir / "update-host.json"
    write(config_path, json.dumps(worker_config), 0o600)
    write(
        layout.units / "dotobot-update-requests.service",
        f"""[Unit]
Description=Process requested Dotobot upgrades
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {layout.root}/current/deploy/update_host.py --config {config_path}
TimeoutStartSec=infinity
""",
        0o644,
    )
    write(
        layout.units / "dotobot-update-requests.timer",
        """[Unit]
Description=Check for owner-requested Dotobot upgrades
[Timer]
OnBootSec=15
OnUnitInactiveSec=15
[Install]
WantedBy=timers.target
""",
        0o644,
    )
    runner(["systemctl", "daemon-reload"])
    runner(["systemctl", "enable", "--now", "dotobot-update-requests.timer"])
