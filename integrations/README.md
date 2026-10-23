# Integrations

Install the core CLI and initialize a private memory home first. Make `amem`
available on the agent's `PATH`, for example with a symlink to the checkout's
shell launcher. Substitute your actual checkout path:

```bash
mkdir -p "$HOME/.local/bin"
ln -s /absolute/path/to/agent-memory/amem "$HOME/.local/bin/amem"
```

If a command already exists at that path, choose another location or inspect it
before replacing it. Ensure `$HOME/.local/bin` is on the agent's `PATH`. A wheel
installation also provides a console command in its environment's `bin` directory.

## Shared skill

[`agent-memory/SKILL.md`](agent-memory/SKILL.md) is the shared instruction file.
It uses `amem` on `PATH` and makes no assumptions about a username or project.
Copy it into the desired client's skill directory from the repository root:

```bash
# Codex personal skill; inspect an existing directory before replacing files.
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/agent-memory/agents"
cp integrations/agent-memory/SKILL.md "${CODEX_HOME:-$HOME/.codex}/skills/agent-memory/SKILL.md"
cp integrations/agent-memory/agents/openai.yaml "${CODEX_HOME:-$HOME/.codex}/skills/agent-memory/agents/openai.yaml"

# Claude Code personal skill.
mkdir -p "$HOME/.claude/skills/agent-memory"
cp integrations/agent-memory/SKILL.md "$HOME/.claude/skills/agent-memory/SKILL.md"
```

For Hermes, register `/absolute/path/to/agent-memory/integrations` as an external
skill directory in the client's configuration. This tree contains one `SKILL.md`
to avoid duplicate discovery. The agent process must inherit `AMEM_HOME` when
using a custom home. Refresh the client's skill discovery after installation.

## Optional stdio MCP

Use the actual launcher or installed console-command path. Put the private home
before `mcp` if it differs from the default:

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "/absolute/path/to/agent-memory/amem",
      "args": ["--home", "/absolute/path/to/private-memory", "mcp"]
    }
  }
}
```

Available tools are `memory_catalog`, `memory_search`, `memory_fetch`, and `memory_locate`.
By default each carries read-only, non-destructive, closed-world annotations
for host approval policies. If `feedback.auto_record_zero_results` is enabled,
`memory_search` is listed as not read-only, since a zero-result search appends a
private feedback row. Use MCP when the host needs structured calls; the CLI
skill is sufficient for agents with shell access.

The MCP server rereads its private `config.json` before each tool call/list.
Disabled sources and changed exclusion/root policies therefore apply to the
next request, including existing refs. Missing or invalid configuration blocks
tools until repaired; the server does not fall back to its startup policy.
Changing `feedback.auto_record_zero_results` requires an MCP restart so the
host receives matching read-only/write annotations. Installing new engine code
also requires restarting existing MCP processes.

## Optional macOS schedule

The launcher wrapper in `launchd/run-incremental-sync` derives the checkout
location from itself and uses `AMEM_HOME` or the default private home. It writes
owner-only logs under the private `.state/logs` and rotates each at 1 MiB.

`launchd/local.agent-memory.incremental-sync.plist` is a template. Replace
`/ABSOLUTE/PATH/TO/agent-memory` with the actual checkout path before installing
it in `~/Library/LaunchAgents`. If needed, add `AMEM_HOME` in its
`EnvironmentVariables` dictionary. Validate with `plutil -lint`, then register
the job using `launchctl`. The template schedules sync at 00:00, 06:00, 12:00,
and 18:00, and at load. Installing it is optional and separate from core setup.

Source navigation and optional semantic candidates are documented in
[the agent contract](../docs/agent-contract.md). `memory_locate` counts toward the
same source-read budget as `memory_fetch`; enabling MCP does not enable semantic
retrieval by default.
