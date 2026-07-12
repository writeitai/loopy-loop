#==================================================================================
# Development Tools
#==================================================================================

.PHONY: claude claudecontinue codex

## Start Claude Code with MCP configuration
claude:
	@test -f .mcp.json || (echo "Error: .mcp.json not found. Please create it first." && exit 1)
	CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 CLAUDE_CODE_ENABLE_AUTO_MODE=1 claude --dangerously-skip-permissions --mcp-config .mcp.json --remote-control

## Continue previous Claude Code session
claudecontinue:
	@test -f .mcp.json || (echo "Error: .mcp.json not found. Please create it first." && exit 1)
	CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 CLAUDE_CODE_ENABLE_AUTO_MODE=1 claude --dangerously-skip-permissions --mcp-config .mcp.json --continue --remote-control

## Codex
codex:
	# do /fast
	codex --yolo --model gpt-5.5

install_globally:
	uv tool install --editable "$(CURDIR)"

install_dev_skills:
	npx skills add https://github.com/anthropics/skills --skill skill-creator -a claude-code -a codex
	npx skills add git@github.com:writeitai/eval-banana.git --skill eval-banana -a codex -a claude-code -y
