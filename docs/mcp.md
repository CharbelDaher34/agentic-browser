# Agentic Browser — MCP server

Give any MCP host (Claude Desktop, Cursor, Cline, or your own agent) a real
browser as one capability.

## Install

```bash
# not yet on PyPI — install from GitHub
uv add "agenticbrowser[mcp] @ git+https://github.com/CharbelDaher34/agentic-browser.git"
uv run python -m agenticbrowser.install   # Chromium for the local backend
```

## Register (Claude Desktop / Cursor)

`claude_desktop_config.json` (or Cursor `mcp.json`):

```json
{
  "mcpServers": {
    "agentic-browser": {
      "command": "uvx",
      "args": ["--from", "agenticbrowser[mcp] @ git+https://github.com/CharbelDaher34/agentic-browser.git", "agenticbrowser-mcp"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "BROWSER_AGENT_BACKEND": "local"
      }
    }
  }
}
```

## Tools

- **`browse_task(goal, session_id="")`** — a complete autonomous browsing run behind
  one call (one round-trip instead of fifty). Pass a `session_id` from
  `open_session` to reuse a logged-in browser; omit it for a one-shot.
- **`open_session()` → `session_id`** — a persistent browser that survives across
  `browse_task` calls (log in once, reuse). Close with **`close_session(session_id)`**.
- **`list_sessions()`** — open persistent session ids.

```python
# programmatic host (PydanticAI) example
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio

browser = MCPServerStdio("uvx",
                         args=["--from", "agenticbrowser[mcp] @ git+https://github.com/CharbelDaher34/agentic-browser.git",
                               "agenticbrowser-mcp"],
                         env={"ANTHROPIC_API_KEY": key})
my_agent = Agent("anthropic:claude-sonnet-4-6", toolsets=[browser])
async with my_agent:
    r = await my_agent.run("Go to news.ycombinator.com and summarize the top thread.")
```

## Approval = elicitation

When the agent hits a destructive step (pay / buy / delete / send / checkout), the
host is asked to approve via MCP **elicitation**. If the host can't elicit, the
action is **auto-denied** (fail-safe) — set `BROWSER_AGENT_AUTO_APPROVE=true` to
allow destructive actions in trusted/unattended setups instead.

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | — | your provider key(s) |
| `BROWSER_AGENT_BACKEND` | `local` | `local` (Chromium) or `browserbase` |
| `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID` | — | for `browserbase` |
| `AGENT_MODEL` | sonnet | orchestrator model, e.g. `anthropic:claude-sonnet-4-6` |
| `WORKER_MODEL` | = AGENT_MODEL | sub-agent model (optionally cheaper) |
| `BROWSER_AGENT_HEADLESS` | `true` | local backend headless |
| `BROWSER_AGENT_SUBAGENTS` | `false` | allow parallel sub-agents |
| `BROWSER_AGENT_MAX_STEPS` | — | budget: max tool steps per task |
| `BROWSER_AGENT_AUTO_APPROVE` | `false` | approve destructive actions when the host can't elicit |
| `BROWSER_AGENT_MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http` |

## Remote transport

```bash
BROWSER_AGENT_MCP_TRANSPORT=streamable-http \
  uvx --from "agenticbrowser[mcp] @ git+https://github.com/CharbelDaher34/agentic-browser.git" agenticbrowser-mcp
```
