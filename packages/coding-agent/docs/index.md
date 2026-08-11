# Oh My Prime Documentation

Oh My Prime is a verified recursive agent runtime built on Prime Agent's persistent IPython kernel, recursive subagents, durable sessions, and multi-process local runtime. It adds isolated speculative execution, deterministic evidence gates, exact-patch promotion, semantic routing, repository intelligence, proof-backed learning, durable task graphs, and capability-constrained child kernels.

## Quick Start

Run the fork from source on Linux or macOS:

```bash
git clone https://github.com/oxura/oh-my-prime.git
cd oh-my-prime
npm install
./prime-agent.sh
```

The fork currently retains the `prime-agent` command and configuration paths. Authenticate with `/login` for subscription or stored API-key providers, or set an environment variable such as `ANTHROPIC_API_KEY` before launch.

## Verified Runtime

- [ProofTree](../skills/prime/SKILL.md) - explore strategy-diverse implementations and select only a fully verified branch.
- [Verifier Fabric](../skills/prove/SKILL.md) - commit-bound acceptance contracts, bug reproducers, evidence ledgers, and exact-patch promotion.
- [Workspace Manager](../skills/workspace/SKILL.md) - durable isolated Git worktrees and stale-safe promotion.
- [Model Mesh](../skills/models/SKILL.md) - semantic routes, capability-aware selection, local verified outcomes, and maker/checker separation.
- [Code Atlas](../skills/atlas/SKILL.md) - semantic repository graph, provenance-rich context capsules, and impact analysis.
- [Evolution Lab](../skills/evolve/SKILL.md) - verified memory candidates, replay, shadow evaluation, promotion, and rollback.
- [Flow Runtime](../skills/flow/SKILL.md) - durable task graphs, typed artifacts, budgets, retries, resource locks, quorum joins, and recovery.

## Start Here

- [Quickstart](quickstart.md) - install, authenticate, and run a first session.
- [Using Oh My Prime](usage.md) - interactive mode, RLM subagents, slash commands, context files, and CLI reference.
- [Architecture overview](architecture.md) - client, daemon, worker, session, kernel, provider, and storage boundaries.
- [RLM programming model](rlm.md) - programmatic execution, native subagents, Python skills, and durable state.
- [Long-running and background agents](long-running-agents.md) - daemon workers, messaging, heartbeats, goals, schedules, and autonomous mode.
- [Providers](providers.md) - subscription and API-key setup for built-in providers.
- [Settings](settings.md) - global and project settings.
- [Keybindings](keybindings.md) - default shortcuts and custom keybindings.
- [Sessions](sessions.md) - session management, branching, and tree navigation.
- [Compaction](compaction.md) - context compaction and branch summarization.

## Customization

- [Extensions](extensions.md) - TypeScript modules for tools, commands, events, and custom UI.
- [Skills](skills.md) - markdown and Python-backed skills, including how to ask Prime Agent to create them.
- [MCP integrations](mcp-integrations.md) - use MCP servers through Python skills without expanding the model's tool surface.
- [Prompt templates](prompt-templates.md) - reusable prompts that expand from slash commands.
- [Themes](themes.md) - built-in and custom terminal themes.
- [Prime Agent packages](packages.md) - bundle and share extensions, skills, prompts, and themes.
- [Custom models](models.md) - add model entries for supported provider APIs.
- [Custom providers](custom-provider.md) - implement custom APIs and OAuth flows.

## Programmatic Usage

- [SDK](sdk.md) - embed Prime Agent in Node.js applications.
- [ACP mode](acp.md) - drive Prime Agent from any Agent Client Protocol client.
- [RPC mode](rpc.md) - integrate over stdin/stdout JSONL.
- [JSON event stream mode](json.md) - print mode with structured events.
- [TUI components](tui.md) - build custom terminal UI for extensions.

## Reference

- [Session format](session-format.md) - JSONL session file format, entry types, and SessionManager API.
- [CLI package reference](../README.md) - complete user and CLI reference.

## Platform Setup

- [Windows](windows.md)
- [Termux on Android](termux.md)
- [tmux](tmux.md)
- [Terminal setup](terminal-setup.md)
- [Shell aliases](shell-aliases.md)

## Development

- [Development](development.md) - local setup, configuration, debugging, and validation.
- [Architecture overview](architecture.md) - system topology and end-to-end prompt flow.
- [Daemon Architecture](daemon.md) - supervisor, catalog, worker, lifecycle, and recovery details.
- [Agent Connection Architecture](agent-connection.md) - client/runtime connection boundary.
- [RLM Runtime Architecture](rlm-runtime.md) - ZeroMQ kernel transport and recursive subagent execution.
