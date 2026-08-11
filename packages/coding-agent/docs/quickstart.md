# Quickstart

This page gets you from a source checkout to a useful first Oh My Prime session.

## Install

Use Node.js 22 or newer and npm 11.10 or newer:

Linux child sandboxes require `ripgrep`, `bubblewrap`, and `socat` (`sudo apt install ripgrep bubblewrap socat` or `sudo dnf install ripgrep bubblewrap socat`).

```bash
git clone https://github.com/oxura/oh-my-prime.git
cd oh-my-prime
npm install
OH_MY_PRIME="$(pwd)/prime-agent.sh"
cd /path/to/project
"$OH_MY_PRIME"
```

The fork retains the release command name `prime-agent` and the `~/.prime/agent` configuration path. A source checkout does not install that command globally; the examples below use the absolute `OH_MY_PRIME` source launcher defined above.

## Authenticate

Oh My Prime can use subscription providers through `/login`, or API-key providers through environment variables or its auth file.

### Option 1: Subscription Login

Start Oh My Prime and run:

```text
/login
```

Then select a provider. Built-in subscription logins include Claude Pro/Max, ChatGPT Plus/Pro (Codex), and GitHub Copilot.

### Option 2: API Key

Set an API key before launching Oh My Prime:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
"$OH_MY_PRIME"
```

You can also run `/login` and select an API-key provider to store the key in `~/.prime/agent/auth.json`.

See [Providers](providers.md) for all supported providers, environment variables, and cloud-provider setup.

## First Session

Once Oh My Prime starts, type a request and press Enter:

```text
Summarize this repository and tell me how to run its checks.
```

Oh My Prime gives the top-level model one built-in tool, `ipython`. The long-lived kernel is a control environment for reading and editing files, running project commands, inspecting data, retaining Python state, and invoking installed skills. The kernel runtime is bootstrapped automatically on first use; set `PRIME_AGENT_KERNEL_PYTHON` to use an existing Python environment with `ipykernel`.

The top-level session runs with your user permissions. Recursive child kernels receive fail-closed capability manifests for their workspace, network, secrets, and process resources; host-side extensions and custom tools remain outside that boundary.

## Recursive Subagents

Recursive subagents are built into Oh My Prime. The model spawns independent work from IPython with `await rlm("subtask")`; each call returns at admission with a child handle and never returns the answer. Children send requested results as explicit `agent_message` replies to the parent or write them to files. Every child uses the same complete agent runtime in its selected workspace and receives an immutable capability manifest that nested children may narrow but never widen.

You can prompt the model to use that capability directly:

```text
Review authentication and test coverage as independent subtasks. Run them in parallel, then synthesize the findings.
```

See [RLM Runtime Architecture](rlm-runtime.md) for the API and execution model.

## Give Oh My Prime Project Instructions

Oh My Prime loads context files at startup. Add an `AGENTS.md` file to tell it how to work in a project:

```markdown
# Project Instructions

- Run `npm run check` after code changes.
- Do not run production migrations locally.
- Keep responses concise.
```

Oh My Prime loads:

- `~/.prime/agent/AGENTS.md` for global instructions
- `AGENTS.md` or `CLAUDE.md` from parent directories and the current directory

Restart Oh My Prime, or run `/reload`, after changing context files.

## Common Things to Try

### Reference Files

Type `@` in the editor to fuzzy-search files, or pass files on the command line:

```bash
"$OH_MY_PRIME" @README.md "Summarize this"
"$OH_MY_PRIME" @src/app.ts @src/app.test.ts "Review these together"
```

Images can be pasted with Ctrl+V (Alt+V on Windows) or dragged into supported terminals.

### Run Shell Commands

In interactive mode:

```text
!npm run lint
```

The command output is sent to the model. Use `!!command` to run a command without adding its output to model context. During agent work, the model normally runs project commands from the IPython control environment with a `%%bash` cell.

### Switch Models

Use `/model` or Ctrl+L to choose a model. Use `/effort` to set the reasoning level. Use Ctrl+P / Shift+Ctrl+P to cycle through scoped models.

### Continue Later

Sessions are saved automatically under `~/.prime/agent/sessions/`:

```bash
"$OH_MY_PRIME" -c                  # Continue the most recent session
"$OH_MY_PRIME" -r [path|id]        # Browse sessions or open a specific session
```

Inside Oh My Prime, use `/resume`, `/new`, `/tree`, `/fork`, and `/clone` to manage sessions. Persistent sessions run in worker processes, so closing the TUI detaches from the agent rather than necessarily stopping it. Use `"$OH_MY_PRIME" agents` to inspect or reattach to active work.

### Non-Interactive Mode

For one-shot prompts:

```bash
"$OH_MY_PRIME" -p "Summarize this codebase"
cat README.md | "$OH_MY_PRIME" -p "Summarize this text"
"$OH_MY_PRIME" -p @screenshot.png "What's in this image?"
```

Use `--mode json` for JSON event output or `--mode rpc` for process integration.

## Next Steps

- [Using Oh My Prime](usage.md) - interactive mode, slash commands, sessions, context files, and CLI reference.
- [Providers](providers.md) - authentication and model setup.
- [Settings](settings.md) - global and project configuration.
- [Keybindings](keybindings.md) - shortcuts and customization.
- [Prime Agent Packages](packages.md) - install shared extensions, skills, prompts, and themes.

Platform notes: [Windows](windows.md), [Termux](termux.md), [tmux](tmux.md), [Terminal setup](terminal-setup.md), [Shell aliases](shell-aliases.md).
