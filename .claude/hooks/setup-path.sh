#!/bin/bash
# Added by tusk install — puts .claude/bin on PATH for Claude Code sessions
if [ -n "$CLAUDE_ENV_FILE" ]; then
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  echo "export PATH=\"$REPO_ROOT/.claude/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
exit 0
