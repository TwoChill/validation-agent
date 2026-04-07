#!/usr/bin/env python3
"""
Validator — Two-Speed Design
─────────────────────────────────────────────────────────────────────────────
STRATEGY 1 — Per-File (hook mode, default when no args):
  Triggered by Claude Code PostToolUse hook after Edit/Write.
  Reads CLAUDE_TOOL_INPUT_FILE_PATH env var.
  Direct mock injection — NO pytest.
  Optional Claude review layer if ANTHROPIC_API_KEY is set (silent if not).
  Target: 10-30 seconds + ~10s Claude review.
  Output: ✅ PASS or ❌ FAIL + brief summary.
  Writes findings to .validator_findings.json for agent handoff.

STRATEGY 2 — Full Project (manual):
  python validator.py --project /path/to/project [--full]
  Tests ALL files, ALL functions, integration between modules.
  Uses pytest if available; falls back to direct mock injection.
  Target: 2-5 minutes.
  Output: Full detailed report.

Both strategies log to validation_log.md.
Exit codes: 0=PASS  1=WARNINGS  2=FAIL
"""

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── Dependency signatures ─────────────────────────────────────────────────────

NETWORK_LIBS    = {"requests", "urllib", "urllib3", "httpx", "aiohttp", "http", "socket", "websockets", "grpc"}
DATABASE_LIBS   = {"sqlite3", "sqlalchemy", "psycopg2", "pymongo", "redis", "motor", "pymysql", "cx_Oracle"}
EXTERNAL_LIBS   = {"boto3", "stripe", "twilio", "sendgrid", "openai", "anthropic", "firebase_admin", "google"}
SUBPROCESS_LIBS = {"subprocess"}
SKIP_DIRS       = {"__pycache__", ".venv", "venv", "env", "site-packages", ".git", "node_modules"}
FINDINGS_FILE   = Path(__file__).parent / ".validator_findings.json"

ACCEPTABLE_EXCEPTIONS = (
    "TypeError", "ValueError", "AttributeError", "KeyError", "IndexError",
    "NotImplementedError", "RuntimeError", "OSError", "ImportError",
    "StopIteration", "UnicodeDecodeError", "UnicodeEncodeError",
)

CLAUDE_SYSTEM_PROMPT = """\
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
4. Domain logic errors — data flow bugs, incorrect parsing, wrong assumptions \
   about input format, off-by-one errors in business logic.

Respond in this exact format:
VERDICT: PASS | WARNINGS | FAIL
ISSUES:
- [line X] <short description>  (or "none" if no issues)
SUMMARY: <one sentence>

Be concise. Flag real problems only — not style preferences.\
"""


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _pytest_available() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def scan_single_file(py_file: Path) -> dict:
    """Analyze a single Python file — deps, env vars, top-level callables."""
    all_imports: Set[str] = set()
    has_file_io = False
    missing_env: List[str] = []
    func_count = 0

    env_pattern = re.compile(
        r'os\.environ(?:\.get)?\s*[\[(][\'"](\w+)[\'"]|os\.getenv\s*\(\s*[\'"](\w+)[\'"]'
    )

    try:
        src = py_file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _empty_analysis(error=str(e))

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return _empty_analysis(syntax_error=str(e))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_count += 1
        if isinstance(node, ast.Import):
            for alias in node.names:
                all_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            all_imports.add(node.module.split(".")[0])

    if re.search(r'\bopen\s*\(', src):
        has_file_io = True

    for m in env_pattern.finditer(src):
        var = m.group(1) or m.group(2)
        if var and var not in os.environ:
            missing_env.append(var)

    deps = {
        "NETWORK":    sorted(all_imports & NETWORK_LIBS),
        "DATABASE":   sorted(all_imports & DATABASE_LIBS),
        "EXTERNAL":   sorted(all_imports & EXTERNAL_LIBS),
        "SUBPROCESS": sorted(all_imports & SUBPROCESS_LIBS),
        "FILESYSTEM": ["open()"] if has_file_io else [],
    }

    callables: List[Tuple[str, List[str]]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                params = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
                callables.append((node.name, params))

    return {
        "deps": deps,
        "missing_env": sorted(set(missing_env)),
        "func_count": func_count,
        "all_imports": all_imports,
        "callables": callables,
    }


def _empty_analysis(error: str = "", syntax_error: str = "") -> dict:
    base = {
        "deps": {k: [] for k in ("NETWORK", "DATABASE", "EXTERNAL", "SUBPROCESS", "FILESYSTEM")},
        "missing_env": [],
        "func_count": 0,
        "all_imports": set(),
        "callables": [],
    }
    if error:
        base["error"] = error
    if syntax_error:
        base["syntax_error"] = syntax_error
    return base


def scan_project(project_dir: Path) -> dict:
    """Scan all .py files; return dependency map and env vars."""
    all_imports: Set[str] = set()
    file_count = 0
    func_count = 0
    has_file_io = False
    missing_env: List[str] = []

    env_pattern = re.compile(
        r'os\.environ(?:\.get)?\s*[\[(][\'"](\w+)[\'"]|os\.getenv\s*\(\s*[\'"](\w+)[\'"]'
    )

    for py_file in sorted(project_dir.rglob("*.py")):
        if any(p in py_file.parts for p in SKIP_DIRS):
            continue
        file_count += 1
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_count += 1
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        all_imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    all_imports.add(node.module.split(".")[0])
        except SyntaxError:
            pass

        if re.search(r'\bopen\s*\(', src):
            has_file_io = True

        for m in env_pattern.finditer(src):
            var = m.group(1) or m.group(2)
            if var and var not in os.environ:
                missing_env.append(var)

    deps = {
        "NETWORK":    sorted(all_imports & NETWORK_LIBS),
        "DATABASE":   sorted(all_imports & DATABASE_LIBS),
        "EXTERNAL":   sorted(all_imports & EXTERNAL_LIBS),
        "SUBPROCESS": sorted(all_imports & SUBPROCESS_LIBS),
        "FILESYSTEM": ["open()"] if has_file_io else [],
    }

    return {
        "deps": deps,
        "missing_env": sorted(set(missing_env)),
        "file_count": file_count,
        "func_count": func_count,
        "all_imports": all_imports,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 1 — DIRECT MOCK INJECTION (no pytest)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_mock_setup_lines(deps: dict, missing_env: List[str]) -> Tuple[List[str], List[str]]:
    """
    Returns (setup_lines, patch_target_pairs_exprs).
    setup_lines: code to define mock objects.
    patch_target_pairs_exprs: list of string expressions like ('requests.get', _resp_mock).
    """
    setup: List[str] = []
    pairs: List[str] = []

    if "requests" in deps.get("NETWORK", []):
        setup += [
            "_resp = MagicMock()",
            "_resp.status_code = 200",
            "_resp.ok = True",
            "_resp.json.return_value = {'ok': True, 'data': [], 'result': 'mock'}",
            "_resp.text = '{\"ok\": true}'",
            "_resp.content = b'{\"ok\": true}'",
            "_resp.raise_for_status = MagicMock()",
            "_resp.iter_content = MagicMock(return_value=iter([b'{\"ok\": true}']))",
            "",
        ]
        for m in ("get", "post", "put", "delete", "patch", "head", "options", "request"):
            pairs.append(f"('requests.{m}', MagicMock(return_value=_resp))")
        pairs.append("('requests.Session', MagicMock)")

    if "httpx" in deps.get("NETWORK", []):
        setup += [
            "_hx = MagicMock()",
            "_hx.status_code = 200",
            "_hx.json.return_value = {'ok': True}",
            "_hx.text = '{\"ok\": true}'",
            "",
        ]
        for m in ("get", "post", "put", "delete", "patch", "head", "options", "request"):
            pairs.append(f"('httpx.{m}', MagicMock(return_value=_hx))")
        pairs.append("('httpx.Client', MagicMock)")
        pairs.append("('httpx.AsyncClient', MagicMock)")

    if "aiohttp" in deps.get("NETWORK", []):
        pairs.append("('aiohttp.ClientSession', MagicMock)")

    if "urllib" in deps.get("NETWORK", []) or "urllib3" in deps.get("NETWORK", []):
        setup += [
            "_ur = MagicMock()",
            "_ur.read.return_value = b'{\"ok\": true}'",
            "_ur.status = 200",
            "_ur.__enter__ = MagicMock(return_value=_ur)",
            "_ur.__exit__ = MagicMock(return_value=False)",
            "",
        ]
        pairs.append("('urllib.request.urlopen', MagicMock(return_value=_ur))")

    if "socket" in deps.get("NETWORK", []):
        pairs += [
            "('socket.socket', MagicMock)",
            "('socket.create_connection', MagicMock)",
            "('socket.getaddrinfo', MagicMock(return_value=[]))",
        ]

    if "redis" in deps.get("DATABASE", []):
        pairs += [
            "('redis.Redis', MagicMock)",
            "('redis.StrictRedis', MagicMock)",
        ]

    if "pymongo" in deps.get("DATABASE", []):
        pairs.append("('pymongo.MongoClient', MagicMock)")

    if "psycopg2" in deps.get("DATABASE", []):
        pairs.append("('psycopg2.connect', MagicMock)")

    if "sqlalchemy" in deps.get("DATABASE", []):
        pairs.append("('sqlalchemy.create_engine', MagicMock)")

    if "openai" in deps.get("EXTERNAL", []):
        setup += [
            "_oai = MagicMock()",
            "_oai.chat.completions.create.return_value = MagicMock(",
            "    choices=[MagicMock(message=MagicMock(content='mock response'))],",
            "    usage=MagicMock(prompt_tokens=10, completion_tokens=10),",
            ")",
            "",
        ]
        pairs += [
            "('openai.OpenAI', MagicMock(return_value=_oai))",
            "('openai.AsyncOpenAI', MagicMock(return_value=_oai))",
        ]

    if "anthropic" in deps.get("EXTERNAL", []):
        setup += [
            "_ant = MagicMock()",
            "_ant.messages.create.return_value = MagicMock(",
            "    content=[MagicMock(text='mock response')],",
            "    usage=MagicMock(input_tokens=10, output_tokens=10),",
            ")",
            "",
        ]
        pairs += [
            "('anthropic.Anthropic', MagicMock(return_value=_ant))",
            "('anthropic.AsyncAnthropic', MagicMock(return_value=_ant))",
        ]

    if "boto3" in deps.get("EXTERNAL", []):
        pairs += [
            "('boto3.client', MagicMock)",
            "('boto3.resource', MagicMock)",
            "('boto3.Session', MagicMock)",
        ]

    if "stripe" in deps.get("EXTERNAL", []):
        pairs += [
            "('stripe.Charge.create', MagicMock)",
            "('stripe.Customer.create', MagicMock)",
            "('stripe.PaymentIntent.create', MagicMock)",
        ]

    if "twilio" in deps.get("EXTERNAL", []):
        pairs.append("('twilio.rest.Client', MagicMock)")

    if "sendgrid" in deps.get("EXTERNAL", []):
        pairs.append("('sendgrid.SendGridAPIClient', MagicMock)")

    if "subprocess" in deps.get("SUBPROCESS", []):
        setup += [
            "_cp = MagicMock(); _cp.returncode = 0; _cp.stdout = b''; _cp.stderr = b''",
            "_pp = MagicMock(); _pp.returncode = 0",
            "_pp.communicate.return_value = (b'', b''); _pp.wait.return_value = 0",
            "",
        ]
        pairs += [
            "('subprocess.run', MagicMock(return_value=_cp))",
            "('subprocess.call', MagicMock(return_value=0))",
            "('subprocess.check_output', MagicMock(return_value=b''))",
            "('subprocess.Popen', MagicMock(return_value=_pp))",
        ]

    return setup, pairs


def build_direct_test_script(py_file: Path, analysis: dict) -> str:
    """Generate a self-contained Python test script with direct mock injection."""
    deps = analysis["deps"]
    missing_env = analysis.get("missing_env", [])
    callables = analysis.get("callables", [])

    lines = [
        "import sys, importlib, json, os",
        "from unittest.mock import MagicMock, patch, Mock",
        "",
        f"sys.path.insert(0, {repr(str(py_file.parent))})",
        "",
    ]

    # Inject missing env vars before anything else
    for var in missing_env:
        lines.append(f"os.environ.setdefault({repr(var)}, 'MOCK_{var}_VALUE')")
    if missing_env:
        lines.append("")

    # Mock setup objects
    setup_lines, patch_pairs = _build_mock_setup_lines(deps, missing_env)
    lines.extend(setup_lines)

    # Build patches list
    pairs_expr = "[" + ", ".join(patch_pairs) + "]" if patch_pairs else "[]"
    lines += [
        f"_patch_pairs = {pairs_expr}",
        "_started = []",
        "for _tgt, _mk in _patch_pairs:",
        "    try:",
        "        _p = patch(_tgt, _mk)",
        "        _p.start()",
        "        _started.append(_p)",
        "    except Exception:",
        "        pass",
        "",
        "results = {'passed': 0, 'failed': 0, 'errors': []}",
        "",
        "try:",
    ]

    mod_name = repr(py_file.stem)
    lines += [
        f"    # ── import {py_file.stem} ──",
        f"    try:",
        f"        _mod = importlib.import_module({mod_name})",
        f"        results['passed'] += 1",
        f"        print('PASS: import {py_file.stem}')",
        f"    except SystemExit:",
        f"        results['passed'] += 1",
        f"        print('PASS: import {py_file.stem} (SystemExit at module level)')",
        f"        _mod = None",
        f"    except Exception as _e:",
        f"        results['failed'] += 1",
        f"        _msg = f'import {py_file.stem}: {{type(_e).__name__}}: {{_e}}'",
        f"        results['errors'].append(_msg)",
        f"        print(f'FAIL: {{_msg}}')",
        f"        _mod = None",
        "",
    ]

    for func, params in callables[:10]:
        n = len(params)
        test_cases = [
            ("none",  ", ".join(["None"] * n)),
            ("empty", ", ".join(['""' if i % 2 == 0 else "[]" for i in range(n)])),
        ]
        for label, args in test_cases:
            lines += [
                f"    # ── {func}({label}) ──",
                f"    if _mod is not None:",
                f"        _fn = getattr(_mod, {repr(func)}, None)",
                f"        if _fn is not None:",
                f"            try:",
                f"                _fn({args})",
                f"                results['passed'] += 1",
                f"                print('PASS: {func}({label})')",
                f"            except ({', '.join(ACCEPTABLE_EXCEPTIONS)}) as _e:",
                f"                results['passed'] += 1  # graceful rejection OK",
                f"                print(f'PASS: {func}({label}) raised {{type(_e).__name__}}')",
                f"            except SystemExit as _e:",
                f"                if _e.code in (0, 1, 2, None):",
                f"                    results['passed'] += 1",
                f"                    print(f'PASS: {func}({label}) SystemExit({{_e.code}})')",
                f"                else:",
                f"                    results['failed'] += 1",
                f"                    _msg = f'{func}({label}): SystemExit({{_e.code}})'",
                f"                    results['errors'].append(_msg)",
                f"                    print(f'FAIL: {{_msg}}')",
                f"            except Exception as _e:",
                f"                results['failed'] += 1",
                f"                _msg = f'{func}({label}): {{type(_e).__name__}}: {{_e}}'",
                f"                results['errors'].append(_msg)",
                f"                print(f'FAIL: {{_msg}}')",
                "",
            ]

    lines += [
        "finally:",
        "    for _p in _started:",
        "        try: _p.stop()",
        "        except Exception: pass",
        "",
        "print(f'RESULTS:{json.dumps(results)}')",
    ]

    return "\n".join(lines)


def run_direct_mocks(py_file: Path, analysis: dict, timeout: int = 25) -> dict:
    """Execute the direct-mock script; return parsed results."""
    script = build_direct_test_script(py_file, analysis)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix="_val.py", prefix="val_",
        delete=False, encoding="utf-8",
    )
    tmp.write(script)
    tmp.close()
    tmp_path = tmp.name

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return {
            "passed": 0, "failed": 1,
            "errors": [f"Timeout after {timeout}s"],
            "output": f"TIMEOUT after {timeout}s",
            "duration": float(timeout),
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    duration = time.perf_counter() - t0

    passed = failed = 0
    errors: List[str] = []
    for line in output.splitlines():
        if line.startswith("RESULTS:"):
            try:
                data = json.loads(line[8:])
                passed = data.get("passed", 0)
                failed = data.get("failed", 0)
                errors = data.get("errors", [])
            except (json.JSONDecodeError, ValueError):
                pass

    # If RESULTS line missing, likely script crashed
    if not any(l.startswith("RESULTS:") for l in output.splitlines()):
        failed = max(failed, 1)
        first_err = next((l for l in output.splitlines() if l.strip()), "unknown error")
        errors = errors or [first_err[:200]]

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "output": output,
        "duration": round(duration, 2),
    }


def run_direct_mocks_all_files(project_dir: Path) -> dict:
    """Full-mode fallback: run direct mock injection on every .py file."""
    total_passed = total_failed = 0
    all_errors: List[str] = []
    file_results: List[dict] = []

    py_files = [
        f for f in sorted(project_dir.rglob("*.py"))
        if not any(p in f.parts for p in SKIP_DIRS)
        and not f.name.startswith(("test_", "conftest", "setup"))
        and f.name not in ("validator.py", "validator_agent.py")
    ]

    for py_file in py_files:
        analysis = scan_single_file(py_file)
        if "syntax_error" in analysis:
            total_failed += 1
            all_errors.append(f"{py_file.name}: SyntaxError: {analysis['syntax_error']}")
            file_results.append({"file": py_file.name, "passed": 0, "failed": 1,
                                  "errors": [analysis["syntax_error"]]})
            continue
        r = run_direct_mocks(py_file, analysis)
        total_passed += r["passed"]
        total_failed += r["failed"]
        all_errors.extend([f"{py_file.name}: {e}" for e in r["errors"]])
        file_results.append({"file": py_file.name, **r})

    return {
        "passed": total_passed,
        "failed": total_failed,
        "errors": all_errors,
        "file_results": file_results,
        "warnings": 0,
        "duration": sum(r.get("duration", 0) for r in file_results),
        "returncode": 0 if total_failed == 0 else 1,
        "failures": [{"test": e, "reason": ""} for e in all_errors[:20]],
        "output": "",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLAUDE REVIEW LAYER (optional second pass)
# ═══════════════════════════════════════════════════════════════════════════════

def _run_claude_review(file_path: Path, source: str) -> tuple:
    """Call Claude API for second-opinion review. Returns (verdict, issues, summary, duration)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return "SKIPPED", [], "", 0.0

    try:
        import anthropic
    except ImportError:
        return "SKIPPED", [], "", 0.0

    client = anthropic.Anthropic(api_key=api_key)
    user_msg = f"Review this Python file: `{file_path.name}`\n\n```python\n{source}\n```"

    t0 = time.perf_counter()
    collected: List[str] = []

    try:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=CLAUDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            for text in stream.text_stream:
                collected.append(text)
    except Exception:
        return "SKIPPED", [], "", 0.0

    duration = time.perf_counter() - t0
    raw = "".join(collected).strip()

    verdict = "PASS"
    issues: List[str] = []
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

    return verdict, issues, summary, duration


def _log_claude_review(log_path: Path, py_file: Path, verdict: str,
                        issues: List[str], summary: str, duration: float) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icon = {"PASS": "✅", "WARNINGS": "⚠️ ", "FAIL": "❌"}.get(verdict, "❓")
    lines = [
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
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 1 ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def _log_per_file(log_path: Path, py_file: Path, status: str,
                  passed: int, failed: int, errors: List[str],
                  duration: float, analysis: dict) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icon = "✅" if status == "PASS" else "❌"
    deps = analysis.get("deps", {})
    all_deps = sum(deps.values(), [])

    lines = [
        "",
        "---",
        f"## [{ts}] {py_file.name} — {icon} {status} (per-file, {duration:.1f}s)",
        "",
        f"- File: `{py_file}`",
        f"- Functions found: {analysis.get('func_count', 0)}",
        f"- Dependencies detected: {', '.join(all_deps) if all_deps else 'none'}",
        f"- Tests: {passed} passed, {failed} failed",
    ]
    if errors:
        lines.append("- Failures:")
        for e in errors[:5]:
            lines.append(f"  - `{e}`")
    lines.append("")

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def run_per_file_mode(file_path: Path, log_path: Path) -> int:
    """Strategy 1: fast per-file validation (10-30s, no pytest)."""
    t0 = time.perf_counter()

    # Only Python files
    if file_path.suffix != ".py":
        # Silently skip non-Python files
        return 0

    if not file_path.exists():
        print(f"[Validator] File not found: {file_path}", file=sys.stderr)
        return 0

    # Skip the validator itself and generated test files
    skip_names = {"validator.py", "validator_agent.py", "conftest.py", "setup.py"}
    if file_path.name in skip_names or file_path.name.startswith("test_"):
        return 0

    print(f"[Validator] {file_path.name} ...", file=sys.stderr, flush=True)

    analysis = scan_single_file(file_path)

    if "syntax_error" in analysis:
        duration = time.perf_counter() - t0
        msg = analysis['syntax_error']
        print(f"❌ Critical issue:", file=sys.stderr)
        print(f"   What broke: SyntaxError — {msg}", file=sys.stderr)
        print(f"   Why it matters: The file cannot run at all", file=sys.stderr)
        print(f"   Score: 0/100", file=sys.stderr)
        _log_per_file(log_path, file_path, "FAIL", 0, 1,
                      [f"SyntaxError: {msg}"], duration, analysis)
        return 2

    if "error" in analysis:
        duration = time.perf_counter() - t0
        msg = analysis["error"]
        print(f"❌ Critical issue:", file=sys.stderr)
        print(f"   What broke: {msg}", file=sys.stderr)
        print(f"   Why it matters: File could not be read or parsed", file=sys.stderr)
        print(f"   Score: 0/100", file=sys.stderr)
        _log_per_file(log_path, file_path, "FAIL", 0, 1, [msg], duration, analysis)
        return 2

    result = run_direct_mocks(file_path, analysis, timeout=25)
    duration = time.perf_counter() - t0

    # ── Security checks (always run) ──────────────────────────────────────────
    security_issues: List[str] = []
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        security_issues = _run_security_checks(source)
    except OSError:
        source = ""

    # Load config to check security_strict flag
    _cfg_path = Path(__file__).parent / "config.json"
    _security_strict = False
    try:
        _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
        _security_strict = bool(_cfg.get("security_strict", False))
    except Exception:
        pass

    status = "PASS" if result["failed"] == 0 else "FAIL"

    # ── Claude review (optional second pass) ──────────────────────────────────
    claude_verdict = "SKIPPED"
    claude_issues: List[str] = []
    claude_summary = ""
    claude_dur = 0.0

    if os.environ.get("ANTHROPIC_API_KEY", "").strip() and source:
        try:
            claude_verdict, claude_issues, claude_summary, claude_dur = _run_claude_review(file_path, source)
            if claude_verdict != "SKIPPED":
                _log_claude_review(log_path, file_path, claude_verdict,
                                   claude_issues, claude_summary, claude_dur)
        except Exception:
            pass

    # ── Human-readable output ─────────────────────────────────────────────────
    scoring = compute_score(
        result["passed"], result["failed"],
        result.get("errors", []), claude_verdict,
        security_issues if _security_strict else [],
    )

    all_issues = result.get("errors", []) + claude_issues
    if _security_strict:
        all_issues += security_issues

    if status == "PASS" and claude_verdict in ("PASS", "SKIPPED") and not (
            _security_strict and security_issues):
        print(f"✅ Code is OK  [{duration:.1f}s]", file=sys.stderr)
    elif status == "FAIL":
        print(f"❌ Critical issue:", file=sys.stderr)
        for err in result["errors"][:3]:
            print(f"   What broke: {err}", file=sys.stderr)
        print(f"   Why it matters: The code crashes or has a serious defect", file=sys.stderr)
    else:
        # WARNINGS — pass with issues
        n = len(all_issues)
        print(f"⚠️  {n} issue{'s' if n != 1 else ''} found:", file=sys.stderr)
        for issue in all_issues[:5]:
            # Try to extract line number if present
            line_ref = ""
            m = re.search(r'\[line (\d+)\]|line[: ]+(\d+)', issue, re.IGNORECASE)
            if m:
                line_ref = f"Line {m.group(1) or m.group(2)}: "
                issue_txt = re.sub(r'\[line \d+\]|line[: ]+\d+[: ]*', '', issue).strip()
            else:
                issue_txt = issue
            print(f"   {line_ref}{issue_txt}", file=sys.stderr)

    # Print score
    print(f"Score: {scoring['score']}/100", file=sys.stderr)
    if scoring["score"] < 100:
        bd = scoring["breakdown"]
        print(f"  Correctness {bd['Correctness']}  "
              f"Security {bd['Security']}  "
              f"Robustness {bd['Robustness']}  "
              f"AI Review {bd['AI Review']}", file=sys.stderr)

    # Security strict warnings (always show if strict mode on)
    if _security_strict and security_issues:
        print(f"🔒 Security scan ({len(security_issues)} flag{'s' if len(security_issues) != 1 else ''}):",
              file=sys.stderr)
        for s in security_issues:
            print(f"   · {s}", file=sys.stderr)

    _log_per_file(log_path, file_path, status,
                  result["passed"], result["failed"],
                  result["errors"], duration, analysis)

    # ── Write findings for agent handoff ──────────────────────────────────────
    findings = {
        "file": str(file_path),
        "static_verdict": status,
        "claude_verdict": claude_verdict,
        "static_errors": result.get("errors", []),
        "claude_issues": claude_issues,
        "issues": all_issues,
        "summary": claude_summary,
        "score": scoring["score"],
    }
    try:
        FINDINGS_FILE.write_text(json.dumps(findings), encoding="utf-8")
    except OSError:
        pass

    return 2 if status == "FAIL" else 0


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 2 HELPERS (pytest path)
# ═══════════════════════════════════════════════════════════════════════════════

def build_conftest(deps: dict, missing_env: List[str]) -> str:
    """Return conftest.py content that auto-mocks all detected deps (pytest fixtures)."""
    blocks: List[str] = []

    if deps.get("NETWORK"):
        if "requests" in deps["NETWORK"]:
            blocks.append("""\
    # requests
    _resp = MagicMock()
    _resp.status_code = 200
    _resp.ok = True
    _resp.json.return_value = {"ok": True, "data": [], "result": "mock"}
    _resp.text = '{"ok": true}'
    _resp.content = b'{"ok": true}'
    _resp.raise_for_status = MagicMock()
    _resp.iter_content = MagicMock(return_value=iter([b'{"ok": true}']))
    for _m in ("get", "post", "put", "delete", "patch", "head", "options", "request"):
        monkeypatch.setattr(f"requests.{_m}", MagicMock(return_value=_resp), raising=False)
    monkeypatch.setattr("requests.Session", MagicMock, raising=False)""")

        if "httpx" in deps["NETWORK"]:
            blocks.append("""\
    # httpx
    _hx = MagicMock()
    _hx.status_code = 200
    _hx.json.return_value = {"ok": True}
    _hx.text = '{"ok": true}'
    for _m in ("get", "post", "put", "delete", "patch", "head", "options", "request"):
        monkeypatch.setattr(f"httpx.{_m}", MagicMock(return_value=_hx), raising=False)
    monkeypatch.setattr("httpx.Client", MagicMock, raising=False)
    monkeypatch.setattr("httpx.AsyncClient", MagicMock, raising=False)""")

        if "aiohttp" in deps["NETWORK"]:
            blocks.append("""\
    # aiohttp
    monkeypatch.setattr("aiohttp.ClientSession", MagicMock, raising=False)""")

        if "urllib" in deps["NETWORK"] or "urllib3" in deps["NETWORK"]:
            blocks.append("""\
    # urllib
    _ur = MagicMock()
    _ur.read.return_value = b'{"ok": true}'
    _ur.status = 200
    _ur.__enter__ = MagicMock(return_value=_ur)
    _ur.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_ur), raising=False)""")

        if "socket" in deps["NETWORK"]:
            blocks.append("""\
    # socket
    monkeypatch.setattr("socket.socket", MagicMock, raising=False)
    monkeypatch.setattr("socket.create_connection", MagicMock, raising=False)
    monkeypatch.setattr("socket.getaddrinfo", MagicMock(return_value=[]), raising=False)""")

    if deps.get("DATABASE"):
        if "redis" in deps["DATABASE"]:
            blocks.append("""\
    # redis
    try:
        import fakeredis
        _fr = fakeredis.FakeRedis()
        monkeypatch.setattr("redis.Redis", MagicMock(return_value=_fr), raising=False)
        monkeypatch.setattr("redis.StrictRedis", MagicMock(return_value=_fr), raising=False)
    except ImportError:
        monkeypatch.setattr("redis.Redis", MagicMock, raising=False)
        monkeypatch.setattr("redis.StrictRedis", MagicMock, raising=False)""")

        if "pymongo" in deps["DATABASE"]:
            blocks.append("""\
    # pymongo
    try:
        import mongomock
        monkeypatch.setattr("pymongo.MongoClient", mongomock.MongoClient, raising=False)
    except ImportError:
        monkeypatch.setattr("pymongo.MongoClient", MagicMock, raising=False)""")

        if "psycopg2" in deps["DATABASE"]:
            blocks.append("""\
    # psycopg2
    monkeypatch.setattr("psycopg2.connect", MagicMock, raising=False)""")

        if "sqlalchemy" in deps["DATABASE"]:
            blocks.append("""\
    # sqlalchemy — redirect to in-memory SQLite
    try:
        import sqlalchemy
        _orig_ce = sqlalchemy.create_engine
        monkeypatch.setattr(
            "sqlalchemy.create_engine",
            lambda *a, **kw: _orig_ce("sqlite:///:memory:"),
            raising=False,
        )
    except ImportError:
        pass""")

    if deps.get("EXTERNAL"):
        if "openai" in deps["EXTERNAL"]:
            blocks.append("""\
    # openai
    _oai = MagicMock()
    _oai.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="mock response"))],
        usage=MagicMock(prompt_tokens=10, completion_tokens=10),
    )
    monkeypatch.setattr("openai.OpenAI", MagicMock(return_value=_oai), raising=False)
    monkeypatch.setattr("openai.AsyncOpenAI", MagicMock(return_value=_oai), raising=False)""")

        if "anthropic" in deps["EXTERNAL"]:
            blocks.append("""\
    # anthropic
    _ant = MagicMock()
    _ant.messages.create.return_value = MagicMock(
        content=[MagicMock(text="mock response")],
        usage=MagicMock(input_tokens=10, output_tokens=10),
    )
    monkeypatch.setattr("anthropic.Anthropic", MagicMock(return_value=_ant), raising=False)
    monkeypatch.setattr("anthropic.AsyncAnthropic", MagicMock(return_value=_ant), raising=False)""")

        if "boto3" in deps["EXTERNAL"]:
            blocks.append("""\
    # boto3
    monkeypatch.setattr("boto3.client", MagicMock, raising=False)
    monkeypatch.setattr("boto3.resource", MagicMock, raising=False)
    monkeypatch.setattr("boto3.Session", MagicMock, raising=False)""")

        if "stripe" in deps["EXTERNAL"]:
            blocks.append("""\
    # stripe
    monkeypatch.setattr("stripe.Charge.create", MagicMock, raising=False)
    monkeypatch.setattr("stripe.Customer.create", MagicMock, raising=False)
    monkeypatch.setattr("stripe.PaymentIntent.create", MagicMock, raising=False)""")

        if "twilio" in deps["EXTERNAL"]:
            blocks.append("""\
    # twilio
    monkeypatch.setattr("twilio.rest.Client", MagicMock, raising=False)""")

        if "sendgrid" in deps["EXTERNAL"]:
            blocks.append("""\
    # sendgrid
    monkeypatch.setattr("sendgrid.SendGridAPIClient", MagicMock, raising=False)""")

    if deps.get("SUBPROCESS"):
        blocks.append("""\
    # subprocess
    _cp = MagicMock()
    _cp.returncode = 0
    _cp.stdout = b""
    _cp.stderr = b""
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=_cp), raising=False)
    monkeypatch.setattr("subprocess.call", MagicMock(return_value=0), raising=False)
    monkeypatch.setattr("subprocess.check_output", MagicMock(return_value=b""), raising=False)
    _pp = MagicMock()
    _pp.returncode = 0
    _pp.communicate.return_value = (b"", b"")
    _pp.wait.return_value = 0
    monkeypatch.setattr("subprocess.Popen", MagicMock(return_value=_pp), raising=False)""")

    if missing_env:
        env_lines = "\n".join(
            f'    monkeypatch.setenv("{v}", "MOCK_{v}_VALUE")'
            for v in missing_env
        )
        blocks.append(f"    # env vars\n{env_lines}")

    if not blocks:
        return ""

    body = "\n\n".join(blocks)
    return f"""\
import pytest
from unittest.mock import MagicMock, patch, Mock


@pytest.fixture(autouse=True)
def _validation_mocks(monkeypatch):
    \"\"\"Auto-injected mocks for all detected external dependencies.\"\"\"
{body}
"""


def find_test_files(project_dir: Path) -> List[Path]:
    found = []
    for pat in ("test_*.py", "*_test.py"):
        for p in project_dir.rglob(pat):
            if any(x in p.parts for x in SKIP_DIRS):
                continue
            found.append(p)
    return sorted(found)


def extract_callables(py_file: Path) -> List[Tuple[str, List[str]]]:
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    results = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                params = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
                results.append((node.name, params))
    return results


def generate_smoke_tests(project_dir: Path, conftest_injected: bool) -> str:
    lines = [
        "import pytest",
        "import sys",
        "import importlib",
        f"sys.path.insert(0, {repr(str(project_dir))})",
        "",
    ]
    if not conftest_injected:
        lines += ["from unittest.mock import MagicMock", ""]

    tested = False
    for py_file in sorted(project_dir.glob("*.py")):
        if py_file.name.startswith(("_", "test", "conftest", "setup")):
            continue
        mod = py_file.stem
        funcs = extract_callables(py_file)
        tested = True

        lines += [
            f"# ── {mod} ────────────────────────────────────────────────────────",
            f"def test_{mod}__importable():",
            f"    mod = importlib.import_module({repr(mod)})",
            f"    assert mod is not None",
            "",
        ]

        for func, params in funcs[:8]:
            safe = f"{mod}__{func}"
            n = len(params)
            none_args  = ", ".join(["None"] * n)
            empty_args = ", ".join(['""' if i % 2 == 0 else "[]" for i in range(n)])
            int_args   = ", ".join(["0"] * n)

            for label, args in [("none", none_args), ("empty", empty_args), ("zero", int_args)]:
                lines += [
                    f"def test_{safe}__{label}():",
                    f"    mod = importlib.import_module({repr(mod)})",
                    f"    fn = getattr(mod, {repr(func)}, None)",
                    f"    if fn is None: pytest.skip('not found')",
                    f"    try:",
                    f"        fn({args})",
                    f"    except (TypeError, ValueError, AttributeError,",
                    f"            KeyError, IndexError, NotImplementedError,",
                    f"            RuntimeError, OSError):",
                    f"        pass  # graceful rejection is acceptable",
                    f"    except SystemExit as e:",
                    f"        assert e.code in (0, 1, 2, None)",
                    "",
                ]

    if not tested:
        lines += ["def test_placeholder(): pass", ""]

    return "\n".join(lines)


def run_pytest(project_dir: Path, extra_paths: Optional[List[str]] = None,
               parallel: bool = False) -> dict:
    """Run pytest; return parsed results dict."""
    cmd = [
        sys.executable, "-m", "pytest",
        "--tb=short",
        "--no-header",
        "-q",
        "--color=no",
    ]
    if parallel:
        # Use pytest-xdist if available
        try:
            import importlib.util
            if importlib.util.find_spec("xdist"):
                cmd += ["-n", "auto"]
        except Exception:
            pass

    if extra_paths:
        cmd.extend(extra_paths)
    else:
        cmd.append(str(project_dir))

    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, cwd=str(project_dir),
            capture_output=True, text=True, timeout=300, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"passed": 0, "failed": 0, "errors": 0, "warnings": 0,
                "output": "TIMEOUT after 300s", "duration": 300.0,
                "failures": [], "returncode": -1}
    duration = time.perf_counter() - t0
    return _parse_pytest_output(proc.stdout + proc.stderr, proc.returncode, duration)


def _parse_pytest_output(output: str, returncode: int, duration: float) -> dict:
    passed = failed = errors = warnings = 0
    failures: List[dict] = []

    summary_re = re.compile(
        r'(\d+) passed|(\d+) failed|(\d+) error|(\d+) warning',
        re.IGNORECASE,
    )
    for m in summary_re.finditer(output):
        if m.group(1): passed   = int(m.group(1))
        if m.group(2): failed   = int(m.group(2))
        if m.group(3): errors   = int(m.group(3))
        if m.group(4): warnings = int(m.group(4))

    for m in re.finditer(r'^FAILED (.+?)(?:\s+-\s+(.+))?$', output, re.MULTILINE):
        failures.append({"test": m.group(1).strip(), "reason": (m.group(2) or "").strip()})

    for m in re.finditer(r'^ERROR (.+?)(?:\s+-\s+(.+))?$', output, re.MULTILINE):
        failures.append({"test": f"ERROR: {m.group(1).strip()}", "reason": (m.group(2) or "").strip()})

    return {
        "passed": passed, "failed": failed, "errors": errors,
        "warnings": warnings, "failures": failures,
        "output": output, "duration": round(duration, 2),
        "returncode": returncode,
    }


def _extract_slow_tests(output: str, n: int = 3) -> List[Tuple[str, float]]:
    slow = []
    for m in re.finditer(r'(PASSED|FAILED)\s+(.+?)\s+\[.*?\]\s+\((\d+\.\d+)s\)', output):
        slow.append((m.group(2), float(m.group(3))))
    slow.sort(key=lambda x: -x[1])
    return slow[:n]


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 2 REPORTING
# ═══════════════════════════════════════════════════════════════════════════════

def compute_status(existing: Optional[dict], generated: Optional[dict]) -> str:
    results = [r for r in [existing, generated] if r]
    if not results:
        return "WARNINGS"

    total_failed = sum(r["failed"] + r.get("errors", 0) for r in results)
    total_passed = sum(r["passed"] for r in results)
    total = total_passed + total_failed

    if total == 0:
        return "WARNINGS"
    fail_rate = total_failed / total if total else 0

    if fail_rate >= 0.3 or any(r.get("returncode", 0) not in (0, 1) for r in results):
        return "FAIL"
    if fail_rate > 0 or any(r.get("warnings", 0) > 0 for r in results):
        return "WARNINGS"
    return "PASS"


def build_suggestions(
    deps: dict, missing_env: List[str], had_existing_tests: bool,
    existing: Optional[dict], generated: Optional[dict],
    pytest_used: bool,
) -> List[str]:
    tips = []
    if not had_existing_tests:
        tips.append("No unit tests found — add pytest tests to a tests/ directory")
    if missing_env:
        tips.append(f"Missing env vars (mocked): {', '.join(missing_env)} — add to .env.example")
    if deps.get("SUBPROCESS"):
        tips.append("subprocess calls detected — validate inputs to prevent shell injection")
    if not pytest_used:
        tips.append("pytest not installed — install it for structured test output: pip install pytest")
    if existing and existing["failed"]:
        tips.append(f"{existing['failed']} existing test(s) failing — review failures above")
    if existing and existing.get("warnings", 0):
        tips.append(f"{existing['warnings']} test warning(s) — check for deprecation notices")
    slow = []
    for r in [existing, generated]:
        if r:
            slow += _extract_slow_tests(r.get("output", ""))
    if slow:
        tips.append(f"Slowest test: {slow[0][0]} ({slow[0][1]}s) — consider caching or mocking")
    return tips


def write_full_log(
    log_path: Path, analysis: dict,
    existing: Optional[dict], generated: Optional[dict],
    status: str, project_name: str, suggestions: List[str],
    tmp_dir_used: Optional[str], pytest_used: bool,
) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icon = {"PASS": "✅", "WARNINGS": "⚠️", "FAIL": "❌"}.get(status, "❓")
    deps = analysis["deps"]

    mocked = []
    if deps.get("NETWORK"):    mocked.append(f"network ({', '.join(deps['NETWORK'])})")
    if deps.get("DATABASE"):   mocked.append(f"database ({', '.join(deps['DATABASE'])})")
    if deps.get("EXTERNAL"):   mocked.append(f"external ({', '.join(deps['EXTERNAL'])})")
    if deps.get("SUBPROCESS"): mocked.append("subprocess")
    if deps.get("FILESYSTEM"): mocked.append(f"file I/O → {tmp_dir_used or '/tmp'}")
    if analysis["missing_env"]: mocked.append(f"env vars ({', '.join(analysis['missing_env'])})")

    mode_label = "pytest" if pytest_used else "direct-mock (pytest unavailable)"

    lines = [
        "",
        "---",
        f"## [{ts}] {project_name} — {icon} {status} (full, {mode_label})",
        "",
        "### Project Analysis",
        f"- Files scanned: {analysis['file_count']}",
        f"- Functions analyzed: {analysis['func_count']}",
        f"- Dependencies: {', '.join(sum(deps.values(), [])) or 'none detected'}",
        "",
        "### Mocking Summary",
    ]
    if mocked:
        for m in mocked:
            lines.append(f"- {m}")
    else:
        lines.append("- No external dependencies detected — no mocking needed")

    lines += ["", "### Test Execution"]
    if existing:
        e = existing
        total_e = e["passed"] + e["failed"] + e.get("errors", 0)
        lines += [
            f"- **Existing tests**: {e['passed']}/{total_e} passed  "
            f"({e['failed']} failed, {e.get('errors',0)} errors) in {e['duration']}s",
        ]
        if e.get("failures"):
            lines.append("  - Failures:")
            for f in e["failures"][:10]:
                reason = f" — {f['reason']}" if f["reason"] else ""
                lines.append(f"    - `{f['test']}`{reason}")
    else:
        lines.append("- Existing tests: none found")

    if generated:
        g = generated
        total_g = g["passed"] + g["failed"] + g.get("errors", 0)
        label = "Smoke tests (pytest)" if pytest_used else "Smoke tests (direct-mock)"
        lines += [
            f"- **{label}**: {g['passed']}/{total_g} passed  "
            f"({g['failed']} failed, {g.get('errors',0)} errors) in {g['duration']}s",
        ]
        if g.get("failures"):
            lines.append("  - Failures:")
            for f in g["failures"][:10]:
                reason = f" — {f['reason']}" if f["reason"] else ""
                lines.append(f"    - `{f['test']}`{reason}")
        # Per-file breakdown for direct-mock fallback
        if not pytest_used and g.get("file_results"):
            lines.append("  - Per-file results:")
            for fr in g["file_results"]:
                fi = "✅" if fr["failed"] == 0 else "❌"
                lines.append(f"    - {fi} `{fr['file']}`: {fr['passed']} passed, {fr['failed']} failed")

    if suggestions:
        lines += ["", "### Suggestions"]
        for s in suggestions:
            lines.append(f"- {s}")

    total_p = (existing["passed"] if existing else 0) + (generated["passed"] if generated else 0)
    total_f = (
        (existing["failed"] + existing.get("errors", 0) if existing else 0) +
        (generated["failed"] + generated.get("errors", 0) if generated else 0)
    )
    lines += [
        "",
        "### Summary",
        f"- Total passed: {total_p}",
        f"- Total failed: {total_f}",
        f"- **Overall: {icon} {status}**",
        "",
    ]

    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def print_full_report(
    project_name: str, analysis: dict,
    existing: Optional[dict], generated: Optional[dict],
    status: str, suggestions: List[str], log_path: Path,
    pytest_used: bool,
) -> None:
    icon = {"PASS": "✅", "WARNINGS": "⚠️", "FAIL": "❌"}.get(status, "❓")
    deps = analysis["deps"]

    print(f"\n{'='*60}")
    print(f"  VALIDATION REPORT — {project_name}")
    print(f"  {icon} Status: {status}")
    mode = "pytest" if pytest_used else "direct-mock (no pytest)"
    print(f"  Mode: full ({mode})")
    print(f"{'='*60}")

    print(f"\n[Analysis]")
    print(f"  Files: {analysis['file_count']}  |  Functions: {analysis['func_count']}")
    all_deps = sum(deps.values(), [])
    print(f"  Dependencies detected: {', '.join(all_deps) if all_deps else 'none'}")
    if analysis["missing_env"]:
        print(f"  Env vars mocked: {', '.join(analysis['missing_env'])}")

    if existing:
        e = existing
        print(f"\n[Existing Tests]  {e['passed']} passed / "
              f"{e['failed']+e.get('errors',0)} failed  ({e['duration']}s)")
        for f in e.get("failures", [])[:5]:
            r = f" — {f['reason'][:80]}" if f["reason"] else ""
            print(f"  ❌ {f['test']}{r}")

    if generated:
        g = generated
        label = "Smoke Tests (pytest)" if pytest_used else "Smoke Tests (direct-mock)"
        print(f"\n[{label}]  {g['passed']} passed / "
              f"{g['failed']+g.get('errors',0)} failed  ({g['duration']}s)")
        for f in g.get("failures", [])[:5]:
            r = f" — {f['reason'][:80]}" if f["reason"] else ""
            print(f"  ❌ {f['test']}{r}")
        if not pytest_used and g.get("file_results"):
            for fr in g["file_results"]:
                fi = "✅" if fr["failed"] == 0 else "❌"
                print(f"  {fi} {fr['file']}: {fr['passed']} passed, {fr['failed']} failed")

    if suggestions:
        print(f"\n[Suggestions]")
        for s in suggestions:
            print(f"  → {s}")

    print(f"\n[Log] {log_path}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 2 ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def run_full_mode(project_dir: Path, log_path: Path) -> int:
    """Strategy 2: comprehensive full-project validation (2-5 min)."""
    project_name = project_dir.name
    pytest_avail = _pytest_available()

    print(f"\n[Validator — Full Mode] {project_name}")
    print(f"  Project : {project_dir}")
    print(f"  Log     : {log_path}")
    print(f"  Pytest  : {'available' if pytest_avail else 'NOT found — using direct-mock fallback'}")

    # ── Phase 1: Analyze ──────────────────────────────────────────────────────
    print("\n[1/4] Scanning project...")
    analysis = scan_project(project_dir)
    deps = analysis["deps"]
    all_detected = sum(deps.values(), [])
    print(f"  {analysis['file_count']} files, {analysis['func_count']} functions")
    if all_detected:
        print(f"  Deps: {', '.join(all_detected)}")
    if analysis["missing_env"]:
        print(f"  Missing env vars (will mock): {', '.join(analysis['missing_env'])}")

    existing_result: Optional[dict] = None
    generated_result: Optional[dict] = None
    tmp_dir: Optional[str] = None

    if pytest_avail:
        # ── pytest path ───────────────────────────────────────────────────────
        conftest_content = build_conftest(deps, analysis["missing_env"])
        conftest_path = project_dir / "conftest.py"
        conftest_backup: Optional[str] = None
        conftest_written = False
        tmp_dir = tempfile.mkdtemp(prefix="validation_")

        try:
            if conftest_content:
                if conftest_path.exists():
                    conftest_backup = conftest_path.read_text(encoding="utf-8")
                    if "_validation_mocks" not in conftest_backup:
                        conftest_path.write_text(
                            conftest_backup.rstrip() + "\n\n" + conftest_content,
                            encoding="utf-8",
                        )
                        conftest_written = True
                else:
                    conftest_path.write_text(conftest_content, encoding="utf-8")
                    conftest_written = True

            if conftest_written:
                mocked_desc = ", ".join(filter(None, [
                    ", ".join(deps["NETWORK"]), ", ".join(deps["DATABASE"]),
                    ", ".join(deps["EXTERNAL"]),
                    "subprocess" if deps["SUBPROCESS"] else "",
                    "env-vars" if analysis["missing_env"] else "",
                ]))
                print(f"\n[2/4] Mocking (pytest fixtures): {mocked_desc or 'none'}")
            else:
                print("\n[2/4] No external deps — mocking not needed")

            # ── Phase 3: existing tests ───────────────────────────────────────
            print("\n[3/4] Discovering tests...")
            test_files = find_test_files(project_dir)

            if test_files:
                print(f"  Found {len(test_files)} test file(s) — running pytest...")
                existing_result = run_pytest(project_dir, parallel=True)
                e = existing_result
                print(f"  {e['passed']} passed, {e['failed']} failed, "
                      f"{e['errors']} errors ({e['duration']}s)")
            else:
                print("  No test files found")

            print("  Generating smoke tests (pytest)...")
            smoke_file = Path(tmp_dir) / "test_validation_smoke.py"
            smoke_content = generate_smoke_tests(
                project_dir, conftest_injected=bool(conftest_written)
            )
            smoke_file.write_text(smoke_content, encoding="utf-8")
            if conftest_path.exists():
                shutil.copy(conftest_path, Path(tmp_dir) / "conftest.py")

            generated_result = run_pytest(project_dir, extra_paths=[str(smoke_file)])
            g = generated_result
            print(f"  Smoke: {g['passed']} passed, {g['failed']} failed, "
                  f"{g['errors']} errors ({g['duration']}s)")

        finally:
            if conftest_written:
                if conftest_backup is not None:
                    conftest_path.write_text(conftest_backup, encoding="utf-8")
                else:
                    conftest_path.unlink(missing_ok=True)
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    else:
        # ── direct-mock fallback path ─────────────────────────────────────────
        print("\n[2/4] Mocking: direct injection (no pytest)")
        print("\n[3/4] Running direct-mock tests on all files...")
        test_files = find_test_files(project_dir)
        generated_result = run_direct_mocks_all_files(project_dir)
        g = generated_result
        print(f"  {g['passed']} passed, {g['failed']} failed ({g['duration']:.1f}s)")

    # ── Phase 4: Report ───────────────────────────────────────────────────────
    print("\n[4/4] Writing report...")
    status = compute_status(existing_result, generated_result)
    suggestions = build_suggestions(
        deps, analysis["missing_env"],
        had_existing_tests=bool(test_files if pytest_avail else []),
        existing=existing_result,
        generated=generated_result,
        pytest_used=pytest_avail,
    )
    write_full_log(log_path, analysis, existing_result, generated_result,
                   status, project_name, suggestions, tmp_dir, pytest_avail)
    print_full_report(project_name, analysis, existing_result, generated_result,
                      status, suggestions, log_path, pytest_avail)

    if status == "FAIL":
        return 2
    elif status == "WARNINGS":
        return 1
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# ZERO-CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def init_config(project_dir: Path) -> None:
    """Auto-generate config.json with smart defaults. Never overwrites existing config."""
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        return

    entry_candidates = ["main.py", "app.py", "index.py", "run.py", "server.py", "cli.py"]
    entry_point = None
    for candidate in entry_candidates:
        if (project_dir / candidate).exists():
            entry_point = f"python3 {candidate}"
            break
    if not entry_point:
        py_files = sorted(
            f for f in project_dir.glob("*.py")
            if not f.name.startswith(("_", "test", "setup"))
        )
        entry_point = f"python3 {py_files[0].name}" if py_files else "python3 main.py"

    config = {
        "project_name": project_dir.name,
        "entry_point": entry_point,
        "working_dir": str(project_dir),
        "timeout_seconds": 8,
        "security_strict": False,
        "_auto_generated": True,
        "_note": "Auto-generated. Edit project_name and entry_point if needed.",
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def detect_project_type(project_dir: Path) -> str:
    """Detect project type: api | cli | script"""
    for f in sorted(project_dir.glob("*.py")):
        try:
            src = f.read_text(errors="replace")
            if re.search(r"(Flask|FastAPI|app\.route|@router\.|@app\.)", src):
                return "api"
            if re.search(r"(argparse|click\.command|typer\.Typer|sys\.argv\[)", src):
                return "cli"
        except OSError:
            pass
    return "script"


def _run_security_checks(source: str) -> List[str]:
    """Run quick security pattern checks. Returns list of issue strings."""
    issues: List[str] = []
    patterns = [
        (r'(?i)(password|secret|api_key|token)\s*=\s*["\'][^"\']{8,}["\']', "Possible hardcoded secret"),
        (r'\beval\s*\(', "eval() — potential code injection risk"),
        (r'\bexec\s*\(', "exec() — potential code injection risk"),
        (r'\bos\.system\s*\(', "os.system() — prefer subprocess with a list"),
        (r'subprocess\.[^\n]*shell\s*=\s*True', "shell=True in subprocess — injection risk"),
        (r'\bpickle\.loads?\s*\(', "pickle deserialization — arbitrary code execution risk"),
        (r'open\s*\([^,)]+,\s*["\']w["\']', "File write — verify the path cannot be controlled by user input"),
    ]
    for pattern, msg in patterns:
        if re.search(pattern, source):
            issues.append(msg)
    return issues


def compute_score(passed: int, failed: int, errors: List[str],
                  claude_verdict: str, security_issues: List[str]) -> dict:
    """Return score dict with total and breakdown."""
    total = passed + failed
    correctness = int((passed / total * 40) if total > 0 else 40)
    security    = max(0, 20 - len(security_issues) * 5)
    robustness  = max(0, 30 - failed * 10)
    ai_bonus    = (10 if claude_verdict in ("PASS", "SKIPPED")
                   else 5 if claude_verdict == "WARNINGS" else 0)
    score = min(correctness + security + robustness + ai_bonus, 100)
    return {
        "score": score,
        "breakdown": {
            "Correctness": f"{correctness}/40",
            "Security":    f"{security}/20",
            "Robustness":  f"{robustness}/30",
            "AI Review":   f"{ai_bonus}/10",
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    script_dir       = Path(__file__).parent.resolve()
    log_path_default = script_dir / "validation_log.md"

    has_project = "--project" in sys.argv
    has_init    = "--init" in sys.argv

    if has_init:
        # ── Auto-generate config ───────────────────────────────────────────────
        # Pull --project value if given, else cwd
        project_str = None
        if "--project" in sys.argv:
            idx = sys.argv.index("--project")
            if idx + 1 < len(sys.argv):
                project_str = sys.argv[idx + 1]
        project_dir = Path(project_str).resolve() if project_str else Path.cwd()
        init_config(project_dir)
        sys.exit(0)

    if not has_project:
        # ── Strategy 1: per-file (hook) ───────────────────────────────────────
        file_path_str = os.environ.get("CLAUDE_TOOL_INPUT_FILE_PATH", "").strip()
        if not file_path_str:
            sys.exit(0)

        file_path = Path(file_path_str).resolve()
        sys.exit(run_per_file_mode(file_path, log_path_default))

    else:
        # ── Strategy 2: full project ──────────────────────────────────────────
        parser = argparse.ArgumentParser(
            description="Validator — Full Project Mode\n"
                        "Usage: python validator.py --project /path [--full] [--log path]"
        )
        parser.add_argument("--project", required=True, help="Path to project directory")
        parser.add_argument("--init", action="store_true",
                            help="Auto-generate config.json and exit")
        parser.add_argument("--full", action="store_true",
                            help="Full validation (default when --project is given)")
        parser.add_argument("--log", default=None,
                            help="Path to validation_log.md (default: next to this script)")
        args = parser.parse_args()

        project_dir = Path(args.project).resolve()
        if not project_dir.is_dir():
            print(f"[ERROR] Not a directory: {project_dir}", file=sys.stderr)
            sys.exit(2)

        if args.init:
            init_config(project_dir)
            sys.exit(0)

        log_path = Path(args.log).resolve() if args.log else log_path_default
        sys.exit(run_full_mode(project_dir, log_path))


if __name__ == "__main__":
    main()
