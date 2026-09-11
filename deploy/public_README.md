# Dotobot harness

An Apache-2.0 self-hosted multi-bot agent runtime. You own the server, its data,
provider accounts and operating costs. Dotobot's separately sold Mac and iOS apps
are proprietary clients; the runtime and CLI do not require a Dotobot account.

## Install on your server

Supported installer targets: Ubuntu 24.04, Debian 12, amd64/arm64. Use a
server with enough memory and disk for your bots' browser computers and a public
IPv4 or IPv6 address. Allow inbound TCP 80 and 443. This installer configures Caddy HTTPS
and keeps the harness API on loopback. It does not change an existing harness or
reverse proxy installation.

```sh
curl -fsSL https://dotobot.com/install.sh | sudo sh -s -- --ip YOUR_PUBLIC_IP
```

A domain is optional: use `--domain bots.example.com` instead if its DNS points
to your server. With neither option, enter your public IP or domain at the prompt.
The script installs Python, Docker, a checksum-verified official Caddy release,
the bot-computer image and a systemd service, then checks HTTPS
before printing a private link code. Paste it into **Add server** in the Dotobot
app. Sign into the app to synchronize your linked servers across your devices.
The server never needs your Dotobot account credentials.

Public IP certificates renew automatically through Caddy using
[Let's Encrypt's 160-hour profile](https://letsencrypt.org/docs/profiles/#shortlived).
Keep the server and ports reachable so renewal can succeed. A changing
IP requires updating the server address and linked connection. Private/LAN IPs
and servers behind carrier-grade NAT need a reachable endpoint or your own secure
network setup. Some third-party OAuth services reject IP-based callback addresses;
those integrations may require a domain or a supported app callback.

Your data lives in `/var/lib/harness`; releases live in `/opt/harness/releases`
and `/opt/harness/current` selects the active release. Server settings are in
`/etc/dotobot-server/server.env`. Local settings, state, link keys and Caddy
configuration (`/etc/dotobot-server/Caddyfile`) are retained on installer reruns. View logs with
`journalctl -u dotobot-server -u caddy`.

Updates are manual by default:

```sh
sudo dotobot-server --update
sudo dotobot-server --link
sudo dotobot-server --auto-update       # optional daily updates
sudo dotobot-server --no-auto-update
```

Updates verify the release checksum, build the machine image before switching,
and check API/HTTPS health. Failed updates select the previous release and
image and check its health; failed recovery is reported for manual intervention. State is never rolled backward: releases that migrate SQLite may require
an operator's compatible release or a backup to recover. Back up your server's
state and Docker volumes before upgrading.

Caddy's certificate renewal is always automatic, independently of your choice to
update the Dotobot runtime manually or automatically. Caddy 2.10 or newer is
required for [ACME profile support](https://github.com/caddyserver/caddy/releases/tag/v2.10.0);
the installer verifies its pinned official release package when needed.

Existing manual/LAN setups remain supported. Python 3.11+ is enough to run the
stdlib-only API/CLI from a checkout. Full sandboxed computer use needs Linux and
Docker/Podman with the machine image:

```sh
python3 -m harness --help
python3 -m harness serve
python3 -m harness link
```

Behind your own HTTPS proxy, pass `--public-url https://bots.example.com` to
`serve`/`link`, or set `HARNESS_PUBLIC_URL`. This is the address advertised to
clients and used for OAuth callbacks; the listener can remain on localhost.
Link codes carry a bearer credential: do not post them in issues or logs.

## Develop

The runtime uses only Python's standard library. Install development tools in a
virtual environment, then run from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
python3 -m ruff check harness agent providers connectors isolation channels deploy tests
python3 -m pytest
```

Public tests are an explicitly selected runtime suite with their own collection
floor. Private native-app and commercial-account integration tests stay in the
private development repository. Every behavior change should add regression
coverage. Do not put real credentials or customer data in tests.

## License and security

The harness source in this repository is Apache-2.0. Dotobot's name and artwork
remain subject to their respective trademark/asset rights. The paid native apps
and account backend are not included. See [LICENSE](LICENSE) and
[SECURITY.md](SECURITY.md).

This initial public repository has no GitHub Actions workflows or access to
private runners. Run the checks above locally. Automated contribution checks
must use a separate, disposable environment with no private network or secrets
before they can be enabled.

Before publishing the standard installer, maintainers must configure and verify
`https://releases.dotobot.com/harness/manifest.json` and the release archives it
references. Archives must use that same HTTPS origin; the installer rejects
cross-origin downloads and checks their SHA-256 digests. Preparing this source
tree does not configure the release domain or publish any releases.
