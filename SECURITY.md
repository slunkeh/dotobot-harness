# Security

Report vulnerabilities privately through the repository's GitHub security
advisories. Do not include live link codes, provider keys, transcripts or customer
data in public issues.

Every remote API/WebSocket request requires the harness linking key. The container
installer publishes the API on loopback for local use, or keeps it on its Docker
network behind Caddy HTTPS when a public address is supplied. Public URLs must not
bypass that authentication. Provider and connector credentials remain on the
owner's server. A Dotobot account synchronizes client connection details; it does
not add roles or user-level isolation within a harness home.

Both public IP addresses and domain names can use trusted HTTPS. IP installations
use Let's Encrypt's short-lived certificates with automatic renewal; they never
fall back to a self-signed certificate or disable certificate verification.

The machines backend gives each bot a sandboxed computer. Never mount the harness
home or Docker socket inside a bot's computer. The process backend is an explicit
local-development escape hatch and does not provide those isolation guarantees.

Keep the operating system, Docker and Caddy patched. Container updates are manual; existing
system installs retain their optional automatic updates. A successful download or service restart is not a substitute
for checking your workflows. Keep backups of harness state and Docker volumes.

## Container controller

The Docker installer gives the harness controller access to the local Docker
engine socket. Treat it as a trusted administrator of that engine and host.
The socket is never mounted into bot computers. Each bot receives only its own
home, the shared project volume, and its explicitly granted read-only script
credential directory. Default local installs publish the API on loopback only;
use the HTTPS address options for remote access. Container packaging does not
make an unsupported host operating system secure.

## Conversation evidence and routine credentials

`search_history` reads original messages, decision cards and delivery receipts
for the current bot conversation. It does not grant access to another bot or
conversation, and recovered approvals are evidence, not current authorization.
Explicit human account selections persist across tasks in the same bot chat,
independently of model context and task classification. Explicit account
removal revokes the selection; disabled accounts cannot execute, and replacement
accounts do not inherit it. Migration uses recorded human selections or exact
account IDs in original human messages, never assistant or external text.
Background jobs use their separately bound scope. Changed instructions still
require action permissions to be evaluated again.

`credential_status` reports credential presence and current bot mount availability
without returning values. A path is shown only when its existing bot grant is
mounted and current. Stored or environment-sourced credential rotation refreshes
already granted bot files; it never grants files to other bots.

When policy asks before credential use, a human can explicitly confirm
`request_routine_credential_permission` for one bot, routine configuration and
credential version. Future matching runs can then reuse that consent. Policy
denials still take precedence. Routine changes or credential replacement require
new consent, deletion revokes it, and legacy prompt prose never creates it.
Inspect or revoke saved consent with `list_chat_permissions` and
`revoke_chat_permission` in the chat that granted it. Revocation stops automatic
credential-file materialization under that consent; a file already granted to the
bot is a separate capability and remains subject to script policy. Remove the
credential to revoke existing copies. The generic **Allow all this turn** button
only covers the current turn and does not approve future routine runs.
