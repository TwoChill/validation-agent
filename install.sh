#!/usr/bin/env bash
set -e

INSTALL_DIR="${VALIDATION_AGENT_DIR:-$HOME/agents/validation-agent}"
REPO="https://github.com/TwoChill/validation-agent.git"

echo "Installing validation-agent..."

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
for perm in [
    f"Bash(python3 {install_dir}/validator.py:*)",
    f"Bash(python3 {install_dir}/validator_agent.py:*)",
]:
    if perm not in allow:
        allow.append(perm)

settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
hook_entry = {
    "matcher": "Edit|Write",
    "hooks": [{
        "type": "command",
        "command": f"python3 {project_dir}/hook_validator.py"
    }]
}
existing = settings["hooks"]["PostToolUse"]
already = any(
    any(h.get("command", "").endswith("hook_validator.py") for h in e.get("hooks", []))
    for e in existing
)
if not already:
    existing.append(hook_entry)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print(f"  Hook registered in {settings_path}")
PYEOF

echo ""
echo "Done. validation-agent is ready."
echo ""
echo "It will automatically check your code after every file edit in Claude Code."
echo "No configuration needed."
echo ""
echo "Optional: add AI-powered auto-fix by setting your API key:"
echo "  export ANTHROPIC_API_KEY=\"sk-ant-...\""
echo "  (get one free at https://console.anthropic.com)"
