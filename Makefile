#==================================================================================
# Development Tools
#==================================================================================

.PHONY: claude claudecontinue codex

## Start Claude Code with MCP configuration
claude:
	@test -f .mcp.json || (echo "Error: .mcp.json not found. Please create it first." && exit 1)
	PATH="$(HOME)/.bun/bin:$(PATH)" CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 TELEGRAM_STATE_DIR=$(PWD)/.claude/channels/telegram claude --dangerously-skip-permissions --mcp-config .mcp.json --chrome --channels plugin:telegram@claude-plugins-official

## Continue previous Claude Code session
claudecontinue:
	@test -f .mcp.json || (echo "Error: .mcp.json not found. Please create it first." && exit 1)
	PATH="$(HOME)/.bun/bin:$(PATH)" CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 TELEGRAM_STATE_DIR=$(PWD)/.claude/channels/telegram claude --dangerously-skip-permissions --mcp-config .mcp.json --continue --chrome --channels plugin:telegram@claude-plugins-official

## Codex
codex:
	# do /fast
	codex --yolo --model gpt-5.4

install_globally:
	uv tool install --editable "$(CURDIR)"
