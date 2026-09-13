# Dotobot harness

An Apache-2.0 self-hosted multi-bot agent runtime. You own the server, its data,
provider accounts and operating costs. Dotobot's separately sold Mac and iOS apps
are proprietary clients; the runtime and CLI do not require a Dotobot account.

## Install with Docker

The harness runs in a controller container and manages a separate sandboxed
computer container for each bot. Linux and macOS hosts need Docker with Linux
container support on amd64 or arm64. The host does not need Python, Caddy or a
particular Linux distribution when Docker is already available. This does not
extend the security support lifetime of an end-of-life host OS.

```sh
curl -fsSL https://dotobot.com/install.sh | bash
```

The installer shows live build activity and finishes with one private `dotobot_`
link code to paste into the app. Build diagnostics are retained in `setup.log`
inside the installation directory; redirected output stays plain text. Existing
`harness_` codes remain valid in compatible clients.

The installer reuses a working local Docker engine. If Docker is missing, it
uses Docker's official Linux installation script or downloads Docker Desktop on
macOS. Administrator permission may be requested. On macOS, complete Docker
Desktop's license and permission prompts; the installer does not accept its
terms on your behalf. Docker Desktop has its own supported OS versions and
licensing requirements. An installed engine that cannot be reached is not
replaced. Remote Docker contexts are rejected because filesystem mounts would
refer to a different host. On Windows, use a Linux shell with Docker Desktop
integration; native PowerShell installation is not provided.

By default, the API is available only at `http://127.0.0.1:8765`. The printed
private link code connects an app on the same computer. For a remote server,
supply a public address and allow incoming TCP 80/443:

```sh
curl -fsSL https://dotobot.com/install.sh | bash -s -- --ip YOUR_PUBLIC_IP
```

A domain is optional: use `--domain bots.example.com` instead. This starts a
separate Caddy HTTPS container; the API has no published plaintext port in this
mode. Public IP certificates use Let's Encrypt's short-lived profile and renew
automatically. A private LAN IP cannot receive a public IP certificate. A home
router may need port forwarding; the installer does not provide a tunnel.

Data and the install record live in `~/.local/share/dotobot`. Set `DOTOBOT_HOME`
to choose another dedicated absolute directory, and use that same value for
subsequent commands. The controller mounts its state at the same absolute path
as the Docker host so each bot's read-only credential mount resolves correctly.
The bot home and shared-workspace volumes stay in Docker. Back up both the
installation directory and bot volumes for a complete recovery.

**Trust boundary:** the controller can control the host Docker engine through
its socket, which grants powerful host access. Never expose that socket to the
network or mount it in a bot computer. Bots retain their existing dropped
capabilities, separate homes, and explicitly granted script credentials.

### Update, link and uninstall

```sh
curl -fsSL https://dotobot.com/install.sh | bash -s -- --update
curl -fsSL https://dotobot.com/install.sh | bash -s -- --link
curl -fsSL https://dotobot.com/install.sh | bash -s -- --uninstall
# Permanently remove Dotobot state, bot homes and shared workspace too:
curl -fsSL https://dotobot.com/install.sh | bash -s -- --uninstall --delete-data
```

Updates are manual. The candidate server and computer images build before the
running server stops. Failed replacement restores the previous server when
possible. State is never rolled back: keep backups before an update that
migrates data. Closing a client does not stop the server. Docker restarts the
server after its engine restarts, and the controller starts the saved bot roster.
This also starts bots that were manually stopped, matching the system-service
startup behaviour. Docker Desktop must be running on macOS.
Uninstall retains data by default and never removes Docker or other services.
Images/build cache may remain reusable after uninstall; no broad Docker prune
is performed. `--port NUMBER` selects a different localhost port on first install.

Existing system-service installations retain their original update commands
(`sudo dotobot-server --update`) and data locations. The new bootstrap delegates
to their installed updater; it does not migrate or adopt them. Existing source
checkouts are also left unchanged. The legacy system bootstrap remains in
`deploy/system_install.sh` for maintenance of that installation route.

For development without Docker, Python 3.11+ can run the API/CLI from a checkout:
`python3 -m harness --backend process serve`. This uses shared host processes,
not the separate bot computers of the standard container installation.

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
