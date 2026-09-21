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
python3 -m harness --help
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

GitHub Actions runs these checks on pushes and pull requests using standard
GitHub-hosted Ubuntu runners with Python 3.11, 3.12 and 3.13. Jobs use read-only
repository permissions and no private runners, networks or secrets. CI does not
publish releases or prove full Linux computer-use installation.

The public CI workflow is maintained in `.github/workflows/ci.yml` in this
repository. Preserve it when refreshing an exported source tree; private
repository workflows must not be imported.

Before publishing the standard installer, maintainers must configure and verify
`https://releases.dotobot.com/harness/manifest.json` and the release archives it
references. Archives must use that same HTTPS origin; the installer rejects
cross-origin downloads and checks their SHA-256 digests. Preparing this source
tree does not configure the release domain or publish any releases.

## Optional Jev context selection

Jev by TypeSafe can filter clearly unrelated recalled memories before a bot
answers. It is off by default and needs no Dotobot account. Use your own TypeSafe
API key; TypeSafe bills your account directly. Enabling it sends the current
request and recalled memory excerpts to TypeSafe. It does not replace your chat
model, rewrite stored history or change the existing compaction safeguards.
Uncertain selections stay included. Failed, rate-limited, timed-out or oversized
requests fall back to standard memory.

Compatible apps offer **Settings → Bot → Jev** on Mac and **Settings → Jev** on
iPhone. Connect/test the key, then enable Jev separately. Older apps can use the
authenticated server API:

| Method | Path | JSON body | Purpose |
| --- | --- | --- | --- |
| GET | `/api/jev` | — | `enabled`, `configured`, credential `source`; never the key |
| POST | `/api/jev/key` | `{"key":"YOUR_KEY"}` | Test access, then store key; leaves enablement unchanged |
| POST | `/api/jev/test` | `{}` | Test the configured key with a small billed request |
| PATCH | `/api/jev` | `{"enabled":true}` | Opt in, or use `false` to disable |
| DELETE | `/api/jev/key` | — | Disable and remove the stored key |

Use the harness linking key as the bearer credential and HTTPS outside a trusted
local connection. Keys use the existing private credential store (0600 files),
not settings or chat history. `TYPESAFE_API_KEY` in the server environment is also
supported; it still requires explicit enablement and must be changed/removed in
the environment. No SDK dependency is added. Calls use pinned model
`jev-1.13.0` with a four-second network timeout and a bounded input size.
The chat activity trail records `jev_context` only for attempted calls, including
fallback when the request fails. Confidence filtering is conservative but must
still be evaluated against your own tasks; it is not a guarantee of relevance.
