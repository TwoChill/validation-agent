#!/usr/bin/env python3
"""
Validator Agent — Real AI agent that fixes code.

Claude drives a tool-use loop to read, fix, and verify Python files.
Receives context from validator.py findings when called via hook (--from-hook).

Usage:
  python validator_agent.py <file.py>          # fix a specific file
  python validator_agent.py --from-hook        # reads CLAUDE_TOOL_INPUT_FILE_PATH + findings

If ANTHROPIC_API_KEY is not set:
  Prints one message, then falls back to running validator.py (static mode).

Guardrails:
  Max iterations : 8 tool-call rounds
  Wall timeout   : 120 seconds
  Token tracking : reported after each run
  Cost estimate  : printed at end (Opus 4.6 rates)

Exit codes: 0=PASS/FIXED  1=PARTIAL  2=FAIL
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

AGENT_DIR     = Path(__file__).parent
LOG_PATH      = AGENT_DIR / "validation_log.md"
FINDINGS_FILE = AGENT_DIR / ".validator_findings.json"

MAX_ITERATIONS     = 8
MAX_WALL_SECONDS   = 120
PRICE_INPUT_PER_M  = 15.0   # $ per 1M input tokens  (claude-opus-4-6)
PRICE_OUTPUT_PER_M = 75.0   # $ per 1M output tokens


# ── Tools ─────────────────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read a Python file and return its full contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_code",
        "description": "Run a Python file with the system interpreter and return stdout + stderr (max 3000 chars).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the Python file to run"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_fix",
        "description": "Write corrected Python source code to a file, replacing its current contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to write to"},
                "fixed_code": {"type": "string", "description": "Complete corrected Python source"},
            },
            "required": ["path", "fixed_code"],
        },
    },
]


def _tool_read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"ERROR: {e}"


def _tool_run_code(path: str) -> str:
    try:
        result = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        out = (result.stdout + result.stderr).strip()
        return out[:3000] if len(out) > 3000 else out or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: Timed out after 15s"
    except Exception as e:
        return f"ERROR: {e}"


def _tool_write_fix(path: str, fixed_code: str) -> str:
    try:
        Path(path).write_text(fixed_code, encoding="utf-8")
        return f"Written {len(fixed_code)} bytes to {path}"
    except OSError as e:
        return f"ERROR: {e}"


TOOLS = {
    "read_file": _tool_read_file,
    "run_code":  _tool_run_code,
    "write_fix": _tool_write_fix,
}


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(target_file: Path, verdict: str, iterations: int,
         total_input: int, total_output: int, cost: float) -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icon = {"FIXED": "✅", "NO_ISSUES": "✅", "PARTIAL": "⚠️ ", "FAILED": "❌"}.get(verdict, "❓")
    lines = [
        "",
        "---",
        f"## [{ts}] {target_file.name} — {icon} Agent {verdict}",
        "",
        f"- File: `{target_file}`",
        f"- Iterations: {iterations}/{MAX_ITERATIONS}",
        f"- Tokens: {total_input:,} input / {total_output:,} output",
        f"- Estimated cost: ~${cost:.4f}",
        "",
    ]
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


# ── Fallback: run validator.py standalone ─────────────────────────────────────

def _run_standalone(target_file: Path) -> int:
    spec = importlib.util.spec_from_file_location(
        "validator", AGENT_DIR / "validator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run_per_file_mode(target_file, LOG_PATH)


# ── Agent loop ────────────────────────────────────────────────────────────────

def run_agent(target_file: Path, findings: Optional[dict] = None) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print(
            "[Agent] No API key detected — running static validator only.",
            file=sys.stderr, flush=True,
        )
        return _run_standalone(target_file)

    try:
        import anthropic
    except ImportError:
        print(
            "[Agent] anthropic package not installed — pip install anthropic",
            file=sys.stderr, flush=True,
        )
        return _run_standalone(target_file)

    client = anthropic.Anthropic(api_key=api_key)

    # Build context from validator.py findings
    context = ""
    if findings and findings.get("issues"):
        issue_list = "\n".join(f"  - {i}" for i in findings["issues"])
        static_v = findings.get("static_verdict", "")
        claude_v = findings.get("claude_verdict", "SKIPPED")
        context = (
            f"\n\nThe validator already ran and found these issues:\n{issue_list}\n"
            f"Static verdict: {static_v}  |  Claude review: {claude_v}\n"
            f"Focus on fixing the confirmed issues above."
        )

    initial_prompt = (
        f"You are a code-fixing agent. Your task: fix real bugs in `{target_file}`.\n"
        f"Steps: read the file → identify bugs → write fix → run to verify → repeat if needed.{context}\n\n"
        f"Rules:\n"
        f"- Only fix actual bugs. Do not refactor, add comments, or change style.\n"
        f"- When the file runs without errors and logic is correct, stop.\n"
        f"- Be concise in your reasoning between tool calls."
    )

    messages = [{"role": "user", "content": initial_prompt}]

    total_input  = 0
    total_output = 0
    iterations   = 0
    wrote_fix    = False
    wall_start   = time.monotonic()
    seen_fix_hashes: set = set()   # repeated-fix detection
    last_error_count: Optional[int] = None  # no-improvement detection

    print(f"[Agent] Starting on {target_file.name} ...", file=sys.stderr, flush=True)

    while iterations < MAX_ITERATIONS:
        if time.monotonic() - wall_start > MAX_WALL_SECONDS:
            print(
                f"[Agent] ⏱ Wall timeout after {MAX_WALL_SECONDS}s — stopping.",
                file=sys.stderr, flush=True,
            )
            verdict = "PARTIAL"
            _log(target_file, verdict, iterations, total_input, total_output,
                 _cost(total_input, total_output))
            return 1

        iterations += 1

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        total_input  += response.usage.input_tokens
        total_output += response.usage.output_tokens

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and block.text.strip():
                    print(f"[Agent] {block.text[:300]}", file=sys.stderr, flush=True)
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    print(
                        f"[Agent]   → {block.name}({list(block.input.keys())})",
                        file=sys.stderr, flush=True,
                    )
                    result = TOOLS[block.name](**block.input)
                    if block.name == "write_fix":
                        wrote_fix = True
                        # Check for repeated fix
                        fix_code = block.input.get("fixed_code", "")
                        fix_hash = hashlib.md5(fix_code.encode()).hexdigest()
                        if fix_hash in seen_fix_hashes:
                            print(
                                "[Agent] Same fix repeated — stopping to avoid loop.",
                                file=sys.stderr, flush=True,
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            })
                            messages.append({"role": "assistant", "content": response.content})
                            messages.append({"role": "user", "content": tool_results})
                            verdict = "PARTIAL"
                            _log(target_file, verdict, iterations, total_input, total_output,
                                 _cost(total_input, total_output))
                            return 1
                        seen_fix_hashes.add(fix_hash)
                    tool_results.append({
                        "type": "tool_use_id" if False else "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            break  # unexpected stop reason

    if iterations >= MAX_ITERATIONS:
        print(f"[Agent] ⚠ Max iterations ({MAX_ITERATIONS}) reached.", file=sys.stderr, flush=True)
        verdict = "PARTIAL"
    else:
        verdict = "FIXED" if wrote_fix else "NO_ISSUES"

    cost = _cost(total_input, total_output)
    icon = "✅" if verdict in ("FIXED", "NO_ISSUES") else "⚠️ "
    print(
        f"[Agent] {icon} {verdict} — {iterations} round(s), "
        f"{total_input:,}↑ {total_output:,}↓ tokens, ~${cost:.4f}",
        file=sys.stderr, flush=True,
    )

    _log(target_file, verdict, iterations, total_input, total_output, cost)
    return 0 if verdict in ("FIXED", "NO_ISSUES") else 1


def _cost(inp: int, out: int) -> float:
    return (inp / 1_000_000 * PRICE_INPUT_PER_M) + (out / 1_000_000 * PRICE_OUTPUT_PER_M)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    from_hook = "--from-hook" in sys.argv

    if from_hook:
        raw_path = os.environ.get("CLAUDE_TOOL_INPUT_FILE_PATH", "").strip()
        if not raw_path:
            sys.exit(0)
        target_file = Path(raw_path).resolve()

        findings = None
        if FINDINGS_FILE.exists():
            try:
                findings = json.loads(FINDINGS_FILE.read_text(encoding="utf-8"))
                FINDINGS_FILE.unlink(missing_ok=True)   # consume — one use only
            except Exception:
                pass

    elif len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        target_file = Path(sys.argv[1]).resolve()
        findings = None

    else:
        raw_path = os.environ.get("CLAUDE_TOOL_INPUT_FILE_PATH", "").strip()
        if not raw_path:
            print(
                "[Agent] No file specified.\n"
                "Usage: python validator_agent.py <file.py>",
                file=sys.stderr,
            )
            sys.exit(1)
        target_file = Path(raw_path).resolve()
        findings = None

    if not target_file.exists():
        print(f"[Agent] File not found: {target_file}", file=sys.stderr)
        sys.exit(2)

    if target_file.suffix != ".py":
        sys.exit(0)

    skip = {"validator.py", "validator_agent.py", "conftest.py", "setup.py"}
    if target_file.name in skip or target_file.name.startswith("test_"):
        sys.exit(0)

    sys.exit(run_agent(target_file, findings))


if __name__ == "__main__":
    main()
