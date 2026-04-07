#!/usr/bin/env python3
"""
hook_validator.py — Claude Code PostToolUse dispatcher.
Place this file in your project root. Do not edit.

Auto-selects mode:
  No API key  → static analysis only
  API key set → static analysis + AI review + auto-fix on failure
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Find the validation-agent directory
_HERE = Path(__file__).resolve().parent
AGENT_DIR = None
for _candidate in [
    _HERE.parent / "agents" / "validation-agent",
    Path.home() / "agents" / "validation-agent",
    Path("/root/agents/validation-agent"),
    _HERE,
]:
    if (_candidate / "validator.py").exists():
        AGENT_DIR = _candidate
        break

if AGENT_DIR is None:
    print("[Validator] Could not find validator.py. Run install.sh to set up.", file=sys.stderr)
    sys.exit(0)

VALIDATOR    = AGENT_DIR / "validator.py"
AGENT_SCRIPT = AGENT_DIR / "validator_agent.py"
CONFIG       = AGENT_DIR / "config.json"


def _has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def main() -> int:
    file_path = os.environ.get("CLAUDE_TOOL_INPUT_FILE_PATH", "")
    if not file_path or not file_path.endswith(".py"):
        return 0

    env = {**os.environ}

    # Auto-init config if missing
    if not CONFIG.exists():
        subprocess.run(
            [sys.executable, str(VALIDATOR), "--init",
             "--project", str(Path(file_path).parent)],
            env=env, capture_output=True,
        )

    # Run Layer 1 — static analysis
    r1 = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        env={**env, "CLAUDE_TOOL_INPUT_FILE_PATH": file_path},
        capture_output=True, text=True,
    )
    sys.stderr.write(r1.stderr)
    sys.stdout.write(r1.stdout)

    # If critical failure and API key present → trigger self-healing agent
    if r1.returncode == 2 and _has_api_key() and AGENT_SCRIPT.exists():
        sys.stderr.write("\n[Auto-fix] Issue detected — running AI repair...\n")
        r2 = subprocess.run(
            [sys.executable, str(AGENT_SCRIPT), file_path],
            env=env, capture_output=True, text=True,
        )
        sys.stderr.write(r2.stderr)
        sys.stdout.write(r2.stdout)
        return r2.returncode

    return r1.returncode


if __name__ == "__main__":
    sys.exit(main())
