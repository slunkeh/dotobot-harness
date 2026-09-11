# Design influences and boundaries

Dotobot is an independent implementation, not a fork or wrapper of Hermes Agent.
Its design draws on common agent patterns: named profiles, skills, persistent
memory, tool calls and messages between agents. The runtime has no Hermes
dependency.

## Runtime ownership

The harness owns bots and their work. Each bot runs in a background process;
closing a client does not stop an active task. A roster records bot identities,
provider choices and instructions. Messages between bots use the same governed
tool path as other actions.

One harness home is one trust boundary. Its bearer key authorizes clients;
bot names, request identifiers and conversation identifiers only select resources
inside that boundary. Separate owners use separate harness homes.

## Data and tools

Conversation and task state persist in SQLite. Configuration and credentials
remain separate, and configuration exports exclude credentials. Skills are
directories containing a `SKILL.md` file; facts and conversation history provide
context for later tasks.

Every tool call passes through the governance layer. Provider and connector
implementations use explicit credential access rather than exposing all host
environment variables to a bot's shell.

## Bot computers

The default machines backend gives each bot a persistent container with a private
home. A shared workspace volume supports collaboration. The harness process
drives the computer from outside its container; the harness home and credentials
are not mounted into it. The process backend is available for explicitly chosen
local setups. The VM backend is not implemented.

## Public and private components

The Apache-2.0 runtime and CLI work without a Dotobot account or app purchase.
The separately distributed native apps and account connection-sync service are
proprietary. An account stores connections to user-managed servers; it does not
own their processes or files.
