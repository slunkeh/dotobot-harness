# Security

Report vulnerabilities privately through the repository's GitHub security
advisories. Do not include live link codes, provider keys, transcripts or customer
data in public issues.

Every remote API/WebSocket request requires the harness linking key. The standard
installer binds the API to localhost behind Caddy HTTPS. Public URLs must not
bypass that authentication. Provider and connector credentials remain on the
owner's server. A Dotobot account synchronizes client connection details; it does
not add roles or user-level isolation within a harness home.

Both public IP addresses and domain names can use trusted HTTPS. IP installations
use Let's Encrypt's short-lived certificates with automatic renewal; they never
fall back to a self-signed certificate or disable certificate verification.

The machines backend gives each bot a sandboxed computer. Never mount the harness
home or Docker socket inside a bot's computer. The process backend is an explicit
local-development escape hatch and does not provide those isolation guarantees.

Keep the operating system, Docker and Caddy patched. Dotobot updates are manual
unless you opt in; a successful download or service restart is not a substitute
for checking your workflows. Keep backups of harness state and Docker volumes.

## Container controller

The Docker installer gives the harness controller access to the local Docker
engine socket. Treat it as a trusted administrator of that engine and host.
The socket is never mounted into bot computers. Each bot receives only its own
home, the shared project volume, and its explicitly granted read-only script
credential directory. Default local installs publish the API on loopback only;
use the HTTPS address options for remote access. Container packaging does not
make an unsupported host operating system secure.
