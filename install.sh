#!/usr/bin/env bash
set -e

INSTALL_DIR="${VALIDATION_AGENT_DIR:-$HOME/agents/validation-agent}"
REPO="https://github.com/TwoChill/validation-agent.git"

echo "Installing validation-agent..."
echo "Tip: run this from your project folder to auto-register the hook there."
echo ""

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --quiet
else
    git clone --quiet "$REPO" "$INSTALL_DIR"
fi

# Install anthropic SDK (silent, optional)
pip install --quiet anthropic 2>/dev/null || true

# Detect project root
PROJECT_DIR="$(pwd)"

# Determine settings.json path
if [ -f "$PROJECT_DIR/.claude/settings.json" ]; then
    SETTINGS="$PROJECT_DIR/.claude/settings.json"
elif [ -f "$HOME/.claude/settings.json" ]; then
    SETTINGS="$HOME/.claude/settings.json"
else
    SETTINGS="$HOME/.claude/settings.json"
    mkdir -p "$HOME/.claude"
fi

# Copy hook dispatcher to project root
cp "$INSTALL_DIR/hook_validator.py" "$PROJECT_DIR/hook_validator.py"

# Auto-generate config.json in agent dir (skips if already exists)
python3 "$INSTALL_DIR/validator.py" --init --project "$PROJECT_DIR" 2>/dev/null || true

# Inject hook into settings.json using Python for safe JSON merge
python3 - <<PYEOF
import json, os, sys

settings_path = "$SETTINGS"
install_dir = "$INSTALL_DIR"
project_dir = "$PROJECT_DIR"

try:
    with open(settings_path) as f:
        settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}

settings.setdefault("permissions", {}).setdefault("allow", [])
allow = settings["permissions"]["allow"]

# Remove stale validator permissions (different install paths from old installs)
allow[:] = [p for p in allow if "validator.py" not in p and "validator_agent.py" not in p]

# Add current install path
for perm in [
    f"Bash(python3 {install_dir}/validator.py:*)",
    f"Bash(python3 {install_dir}/validator_agent.py:*)",
]:
    allow.append(perm)

settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
this_hook_cmd = f"python3 {project_dir}/hook_validator.py"
hook_entry = {
    "matcher": "Edit|Write",
    "hooks": [{"type": "command", "command": this_hook_cmd}]
}
existing = settings["hooks"]["PostToolUse"]

# Check if THIS project's hook is already registered (not just any hook_validator.py)
already = any(
    any(h.get("command") == this_hook_cmd for h in e.get("hooks", []))
    for e in existing
)
if not already:
    existing.append(hook_entry)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print(f"  Hook registered in {settings_path}")
PYEOF

# ── Install git pre-commit hook ───────────────────────────────────────────────
GIT_HOOKS_DIR="$PROJECT_DIR/.git/hooks"

if [ -d "$GIT_HOOKS_DIR" ]; then
    # Back up any existing non-symlink hook so we don't silently overwrite it
    if [ -e "$GIT_HOOKS_DIR/pre-commit" ] && [ ! -L "$GIT_HOOKS_DIR/pre-commit" ]; then
        mv "$GIT_HOOKS_DIR/pre-commit" "$GIT_HOOKS_DIR/pre-commit.bak"
        echo "  Existing pre-commit hook backed up to pre-commit.bak"
    fi
    # Symlink preferred: stays in sync when the agent updates via git pull
    if ln -sf "$INSTALL_DIR/pre-commit" "$GIT_HOOKS_DIR/pre-commit" 2>/dev/null; then
        chmod +x "$INSTALL_DIR/pre-commit"
        echo "  Git pre-commit hook installed: $GIT_HOOKS_DIR/pre-commit"
    else
        cp "$INSTALL_DIR/pre-commit" "$GIT_HOOKS_DIR/pre-commit"
        chmod +x "$GIT_HOOKS_DIR/pre-commit"
        echo "  Git pre-commit hook installed (copy): $GIT_HOOKS_DIR/pre-commit"
    fi
else
    echo "  Note: $PROJECT_DIR is not a git repository — pre-commit hook not installed."
fi

echo ""
echo "Done. validation-agent is ready."
echo ""
echo "Claude Code users  : checks run automatically after every file edit."
echo "All git users      : pre-commit hook validates files before every commit."
echo ""
echo "To bypass a specific commit: git commit --no-verify"
echo ""
echo "Optional: add AI-powered auto-fix by setting your API key:"
echo "  export ANTHROPIC_API_KEY=\"sk-ant-...\""
echo "  (get one free at https://console.anthropic.com)"
