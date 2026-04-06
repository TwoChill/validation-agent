#!/usr/bin/env python3
"""
Claude Validator — AI-powered second-layer code review.

Runs after validator_agent.py passes (cascade mode), or standalone (claude-only mode).
Reads CLAUDE_TOOL_INPUT_FILE_PATH from env, sends code to Claude API, reports findings.

Exit codes: 0=PASS  1=WARNINGS  2=FAIL
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

VALIDATOR_DIR = Path(__file__).parent
LOG_PATH      = VALIDATOR_DIR / "validation_log.md"

SYSTEM_PROMPT = """\
You are a senior code reviewer performing a focused second-pass review. \
The code has already passed syntax checks and basic mock-injection tests. \
Your job is to catch what static analysis cannot.

Review the code for:
1. Intent vs implementation — does the code actually do what it appears to intend? \
   Wrong conditions, inverted logic, off-by-one errors.
2. Security issues with context — not just "requests is used" but specific \
   vulnerabilities: unsanitized user input, SSRF risks, hardcoded secrets, \
   path traversal, injection points.
3. Silent failures — exceptions being swallowed, return values never checked, \
   error paths that succeed silently.
4. Domain logic errors — for this OSINT tool: phone/email input handling, \
   API response parsing, export file writing.

Respond in this exact format:
VERDICT: PASS | WARNINGS | FAIL
ISSUES:
- [line X] <short description>  (or "none" if no issues)
SUMMARY: <one sentence>

Be concise. Flag real problems only — not style preferences.\
"""


def _no_api_key() -> None:
    print("[Claude] ⚠️  Geen ANTHROPIC_API_KEY — Claude review overgeslagen",
          file=sys.stderr, flush=True)


def _log(py_file: Path, verdict: str, issues: list[str],
         summary: str, duration: float) -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icon = {"PASS": "✅", "WARNINGS": "⚠️ ", "FAIL": "❌"}.get(verdict, "❓")

    lines = [
        "",
        "---",
        f"## [{ts}] {py_file.name} — {icon} Claude {verdict} ({duration:.1f}s)",
        "",
        f"- File: `{py_file}`",
        "- Issues:",
    ]
    if issues:
        for issue in issues:
            lines.append(f"  - {issue}")
    else:
        lines.append("  - none")
    lines += [f"- Summary: {summary}", ""]

    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def run(file_path: Path) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        _no_api_key()
        return 0  # caller decides — cascade treats this as non-blocking

    try:
        import anthropic
    except ImportError:
        print("[Claude] ⚠️  anthropic package niet geïnstalleerd — pip install anthropic",
              file=sys.stderr, flush=True)
        return 0

    if not file_path.exists():
        print(f"[Claude] ❌ Bestand niet gevonden: {file_path}", file=sys.stderr, flush=True)
        return 2

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[Claude] ❌ Kan bestand niet lezen: {e}", file=sys.stderr, flush=True)
        return 2

    if not source.strip():
        print("[Claude] ⚠️  Leeg bestand — overgeslagen", file=sys.stderr, flush=True)
        return 0

    client = anthropic.Anthropic(api_key=api_key)

    user_msg = (
        f"Review this Python file: `{file_path.name}`\n\n"
        f"```python\n{source}\n```"
    )

    t0 = time.perf_counter()
    collected = []

    try:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            for text in stream.text_stream:
                collected.append(text)
    except anthropic.AuthenticationError:
        print("[Claude] ❌ Ongeldige ANTHROPIC_API_KEY", file=sys.stderr, flush=True)
        return 2
    except anthropic.RateLimitError:
        print("[Claude] ⚠️  Rate limit — Claude review overgeslagen", file=sys.stderr, flush=True)
        return 0
    except Exception as e:
        print(f"[Claude] ⚠️  API fout: {e}", file=sys.stderr, flush=True)
        return 0

    duration  = time.perf_counter() - t0
    raw       = "".join(collected).strip()

    # ── Parse response ────────────────────────────────────────────────────────
    verdict = "PASS"
    issues  = []
    summary = ""

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            word = line.split(":", 1)[1].strip().upper()
            if word in ("PASS", "WARNINGS", "FAIL"):
                verdict = word
        elif line.startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
        elif line.startswith("- ") and "ISSUES" not in line:
            txt = line[2:].strip()
            if txt.lower() != "none":
                issues.append(txt)

    # ── Stderr output (visible in Claude Code UI) ─────────────────────────────
    icon        = {"PASS": "✅", "WARNINGS": "⚠️ ", "FAIL": "❌"}.get(verdict, "❓")
    issue_count = len(issues)

    if verdict == "PASS":
        print(f"[Claude]    {icon} PASS (~{duration:.0f}s)", file=sys.stderr, flush=True)
    else:
        label = f"{issue_count} issue{'s' if issue_count != 1 else ''}"
        first = f": {issues[0]}" if issues else ""
        print(f"[Claude]    {icon} {verdict} — {label}{first} (~{duration:.0f}s)",
              file=sys.stderr, flush=True)

    _log(file_path, verdict, issues, summary, duration)

    return {"PASS": 0, "WARNINGS": 1, "FAIL": 2}.get(verdict, 0)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    raw_path = os.environ.get("CLAUDE_TOOL_INPUT_FILE_PATH", "").strip()
    if not raw_path:
        print("[Claude] ❌ CLAUDE_TOOL_INPUT_FILE_PATH niet gezet", file=sys.stderr, flush=True)
        sys.exit(2)

    sys.exit(run(Path(raw_path)))
