[![Dotobot — Your AI team. Your space.](docs/assets/dotobot-banner.png)](https://dotobot.com)

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

Updates remain owner-controlled. Supported installations expose **Server updates**
in the Mac/iPhone settings and accept the existing CLI update request. A durable
operation prepares verified artifacts, reconnects the controller, then updates
one affected bot at a time. Current tasks finish first; queued messages remain
held until the bot confirms its runtime and health. Human control and approvals
are never interrupted automatically. A failure pauses the rollout with progress
and recovery details available after reconnecting.

Compatible controller changes preserve running agents. Agent-only changes reuse
the existing computer and its home without copying browser profiles. Image
changes take a verified snapshot and retain the same persistent volume. The
updater retains previous release artifacts; it never restores an old data
snapshot over newer work.

Legacy Docker controllers require an explicit bridge migration. Stop all bots
through the normal lifecycle, then rerun the bootstrap with `--update
--bridge-upgrades`. Recorded agents block the migration. Their state, volumes,
network and address are retained, and stopped bots remain stopped. The new
controller supervisor reloads the server without replacing its container; a
separate, installation-owned worker executes update requests. Controller base-image
or incompatible state/protocol changes require a stopped-fleet migration.

The API advertises `harness_updates_v1`: authenticated `GET /api/updates/preview`
and `GET /api/updates` provide availability and durable progress; `POST
/api/updates/start` accepts the preview's `version`, and `POST /api/updates/retry`
reconciles a paused operation. Old clients continue using existing endpoints.
Release builders pass `--runtime-root` to `deploy/release_manifest.py` to include
verified runtime fingerprints. Missing metadata cannot authorize skipping bot
restarts or automatic rollback. Maintain `UPDATE_PROTOCOL` and `STATE_SCHEMA`
in `harness/runtime_identity.py` when their compatibility contracts change.

System-service installations add a request-polling timer without changing the
existing automatic-release preference. `HARNESS_AUTO_ROLL=0` disables implicit
rolls, while explicitly requested upgrades still proceed. `HARNESS_ROLL_SPACING`
can add a delay between bots; the default is zero because readiness already
serializes the rollout.

Closing a client does not stop the server. Docker Desktop must be running on
macOS. Uninstall retains data by default and never removes Docker or other
services. Images/build cache may remain reusable; no broad Docker prune runs.
`--port NUMBER` selects a different localhost port on first install.

Existing system-service installations retain their original update commands
(`sudo dotobot-server --update`) and data locations. The new bootstrap delegates
to their installed updater; it does not migrate or adopt them. Existing source
checkouts are also left unchanged. The legacy system bootstrap remains in
`deploy/system_install.sh` for maintenance of that installation route.

For development without Docker, Python 3.11+ can run the API/CLI from a checkout:
`python3 -m harness --backend process serve`. This uses shared host processes,
not the separate bot computers of the standard container installation.

## Scheduled work and recovery

A scheduled occurrence records its original due time, timezone and run ID. Its
status distinguishes `queued`, `running`, `waiting`, `completed`, `expired`,
`cancelled`, `failed` and `unknown`. `completed` means the agent finished processing
the run; it does not certify an external publication or other side effect. Test
run requests enqueue work and report the routine's actual enabled state.

Recurring routines default to `missed_run_policy="skip"` with
`max_lateness_seconds=3600`, including existing saved configurations without these
fields. One-shot reminders default to `run_late`. An explicit `run_late` policy
keeps delayed execution enabled; `skip` accepts a configurable grace period.
The window is checked before starting and before further actions, including after
a late approval. Editing or deleting the routine invalidates queued occurrences.
No historical cron ticks are reconstructed when the scheduler was offline.

An unanswered scheduled decision is saved and releases the worker for other work.
Its answer resumes the exact occurrence once, including after a restart. Earlier
generic action fingerprints prevent replay of matching actions; uncertain outcomes
remain held for verification. These receipts contain no command arguments or
result contents and do not grant permissions. Each run retains at most 64 such
fingerprints; further mutations are held when that bound is reached.

On upgrade, already queued scheduled messages that lack occurrence metadata and
have waited over one hour are skipped with an explanatory notice. Their original
due time and reminder type cannot safely be reconstructed. Fresh legacy messages
keep their existing behavior. A skipped item needs a fresh run if still relevant;
this does not edit the routine configuration or credentials. Existing client and
state fields remain supported; new metadata is additive. Custom Python scheduler
send callbacks must accept `task_scope` and `routine` keyword arguments and pass
both to `Orchestrator.chat_stream`, as the built-in server relay does.

Recovery notifications retain their internal origin and refer to the saved source
request. Known unfinished work keeps its current task bindings and normal approval
rules. Completed, stale, missing or uncertain source state cannot authorize a new
action simply because a recovery message arrived.

## Posting decisions

Use `confirm` with `outgoing_message` to show the destination, exact message and
separate review context in one approval card. Accept and Decline are saved as
distinct decisions. `computer_submit_approved` takes only the saved approval ID,
checks the current task and page, and dispatches that proposal at most once.
Changed text or destination requires a new proposal. Existing clients can show
the complete proposal through the ordinary confirmation question/detail fields.

The verified browser path uses the existing Chrome debugging connection
(`HARNESS_CHROME_CDP=1`); it does not require Jev. It supports one visible plain
textarea with a standard same-origin submit form. Unsupported or ambiguous
composers, changed form controls and unknown submission outcomes stop the action.
A dispatched submit still needs independent publication verification. Generic
computer controls remain available for other work; they do not provide this
proposal-binding contract.

Custom blocks reject unsupported button-action fields and differently labelled
buttons that submit the same answer. Form submit actions contain only `kind`;
named actions need distinct IDs when they represent different decisions.

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

## Optional Jev assistance

Jev by TypeSafe can filter clearly unrelated recalled memories before a bot
answers. It is off by default and needs no Dotobot account. Use your own TypeSafe
API key; TypeSafe bills your account directly. Enabling it sends the current
request and recalled memory excerpts to TypeSafe. It does not replace your chat
model or grant permission to act. Additional features are separate opt-ins; merely
enabling Jev never enables them.
Uncertain selections stay included. Failed, rate-limited, timed-out, oversized
or all-omitted selections fall back to standard memory. Authoritative decisions
are retained locally without sending their content to the selector. Selection
diagnostics contain hashed candidate identifiers, not memory text.

Compatible apps offer **Settings → Bot → Jev** on Mac and **Settings → Jev** on
iPhone. Connect/test the key, then enable Jev separately. Older apps can use the
authenticated server API:

| Method | Path | JSON body | Purpose |
| --- | --- | --- | --- |
| GET | `/api/jev` | — | `enabled`, `configured`, credential `source`, and `features`; never the key |
| POST | `/api/jev/key` | `{"key":"YOUR_KEY"}` | Test access, then store key; leaves enablement unchanged |
| POST | `/api/jev/test` | `{}` | Test the configured key with a small billed request |
| PATCH | `/api/jev` | `{"enabled":true}` | Opt in, or use `false` to disable |
| DELETE | `/api/jev/key` | — | Disable and remove the stored key |

### Additional checks (all off by default)

PATCH `/api/jev` with `{"features":{"compaction":true,"memory":true}}` to
select individual features. The main `enabled` switch must also be on. Feature
choices survive disabling Jev, but no feature calls TypeSafe while it is off.
Compatible Mac/iPhone settings expose each supported feature separately.

| Feature key | Behavior and data sent to TypeSafe |
| --- | --- |
| `compaction` | Compare the same bounded conversation excerpt used by the summarizer with its candidate summary. Confidently missing/contradicted constraints, decisions, identifiers or unfinished work trigger existing corrective retries. No passing candidate means no summary commit. Epoch folds with a detected omission are skipped. Original session records remain intact. |
| `memory` | Compare a new memory with the last 30 saved facts. Persist advisory durable/temporary and new/duplicate/conflict labels alongside it; the remember tool reports them. Never silently delete, replace or discard a requested memory. |
| `tool_results` | Filter clearly unrelated entries from large, successful read-only JSON search results (`results`, `items`, `messages`, `events` or `files`). Preserve original retained objects, pagination/count metadata and external-content boundaries; mark omissions. Unsupported shapes, errors, uncertainty, and an all-omitted result keep the original. At most three eligible results are considered per turn. |
| `completion` | Compare a final reply with this turn's original tool receipts. A confidently unsupported claim gets one tool-free answer revision and recheck, never an action retry or contradictory footer. If the main agent used images, earlier receipts or structured history, this text-only review abstains; those records are not additionally sent to TypeSafe. A model judgment is not proof of success or failure. |
| `handoffs` | Offer `recommend_handoff(task)` to the bot. Compare the task with roster roles and the requesting bot's pending handoffs in this conversation; group chats restrict candidates to members. Recommend a bot and flag possible duplicates. Never send, suppress, reroute or authorize a handoff. |
| `notifications` | Classify the final reply's attention needs to order pending push delivery. Approval/input prompts retain top priority. Every notification remains queued; nothing is suppressed. Existing notification preferences and relay payloads remain unchanged. |
| `browser` | Offer `computer_browser(goal, max_steps)` for ordinary HTML navigation and forms. Send the bounded goal, visible page text, controls and recent actions to TypeSafe. Jev selects the operation and compatible target together; the bot's configured model generates literal field text only when needed. Every action passes existing permissions. |

All checks use the owner's key, redact registered secrets, and retain the existing
48 KB request bound and four-second HTTP timeout. There are no paid background
scans. Each extra check can add a request and latency; questions about the same
state are batched. Oversized inputs and failed/invalid answers use baseline
behavior. Confidence below 0.9 is treated as uncertain, not correctness evidence.
Thresholds are conservative starting policies, not measured accuracy guarantees.
The offline suite uses fake judgments; live quality, latency and savings still
need evaluation on representative user tasks. Tool filtering is intentionally
limited to structured result lists, not arbitrary command output or screenshots.

### Fast browser actions (Beta)

Enable **Fast browser actions** in Jev settings, or PATCH `/api/jev` with
`{"features":{"browser":true}}`. It defaults off, requires the main Jev switch
and uses the existing TypeSafe key. Field text uses the bot's configured provider
and its normal usage accounting; no additional API key or browser dependency is needed.

The server must also have `HARNESS_CHROME_CDP=1`. Restart the harness with that
setting, then reopen Chrome so it starts with its loopback debugging endpoint.
`GET /api/jev` reports `browser.cdp_enabled`; this is configuration, not proof of
a live connection. The tool checks the bot's existing profile and connection at
execution time, preserving bot session isolation. It never launches another profile.
With multiple tabs, it selects the sole visible web tab (up to eight candidates);
ambiguous windows or a tab switch return control without guessing a target.

The main agent opens the web page and supplies a bounded goal. Each decision
batches operation and compatible-target questions in one TypeSafe request. An
isolated-world DOM snapshot keeps actual node references; actions consume that
observation once, recheck page/field/option state and reject covered or replaced
controls. Page content and history remain untrusted; registered secrets are redacted
before model calls, and password, payment-autofill and file inputs are excluded.

Runs default to eight decisions (maximum twelve) and stop starting work after
30 seconds; an in-flight provider/browser call can finish after that deadline.
Stops, changed task instructions, permissions and disabling the feature are
checked between steps and after model/approval waits. Failed or uncertain inputs
are never automatically replayed. The result distinguishes dispatched inputs,
fallback and an unknown input outcome. Jev's DONE asks the main agent to verify
the result independently, not claim success.

This first version uses semantic HTML clicks and value changes. Frames, shadow
DOM, canvas, rich editors, uploads, oversized pages and widgets requiring trusted
physical input return to normal visual computer use. It does not handle browser
pop-up tabs or nested scrolling. There are no measured speed or reliability claims.

Design references: [jev-ultrafast](https://github.com/browser-use/jev-ultrafast)
for batched operation/target selection and text-only generation, and
[agent-desktop](https://github.com/lahfir/agent-desktop) for observation-scoped
references, bounded observations and explicit recovery. Neither is a dependency.

Use the harness linking key as the bearer credential and HTTPS outside a trusted
local connection. Keys use the existing private credential store (0600 files),
not settings or chat history. `TYPESAFE_API_KEY` in the server environment is also
supported; it still requires explicit enablement and must be changed/removed in
the environment. No SDK dependency is added. Calls use pinned model
`jev-1.13.0` with a four-second network timeout and a bounded input size.
The chat activity trail records `jev_context` only for attempted calls, including
fallback when the request fails. Confidence filtering is conservative but must
still be evaluated against your own tasks; it is not a guarantee of relevance.

### Optional notification relay

An operator can forward completed replies and input requests to an HTTPS push
relay. The runtime does not contain Apple credentials and does not need a
Dotobot account to run. Without a relay, existing HTTP/WebSocket behavior is
unchanged.

Set `HARNESS_PUSH_RELAY_URL`, or persist `{"url":"https://your-relay.example/push/events"}`
in `$HARNESS_HOME/push-relay.json`, then restart the controller through its
normal lifecycle. The file option survives container/controller upgrades.
The relay URL is operator-controlled; clients cannot change the destination.

An authenticated owner registers a relay-issued capability with
`POST /api/push/subscriptions`, JSON `{"id":"<64 hex characters>","secret":"<64 hex characters>"}`.
The capability authorizes delivery only, never account or harness access.
The relay must bind it to the correct account/server/device, enforce notification
preferences and revocation, and deduplicate `event_id`. No endpoint lists or
returns registered capabilities.

Completed nonempty replies, choices, secrets, takeover requests, confirmation /
control-return cards and blocking blocks enter a private SQLite outbox.
Prompt updates, resolutions and tool-only empty finals do not notify. Group
notifications retain the group title and identify the speaking bot. Delivery
runs off the chat path, refuses redirects, retries temporary failures for up to
one hour, and retires revoked capabilities. Registrations expire after 90 days;
clients renew them. Event receipts are retained for one day, with at most 100
registrations and 10,000 outbox rows. A relay must keep its dedupe identity stable
across retries, including an uncertain network response.
