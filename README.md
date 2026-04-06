# Validation Agent

A two-layer code validator that hooks into Claude Code's `PostToolUse` event. After every file edit, it automatically validates the changed file and reports directly in the Claude Code UI.

**Layer 1** — Fast static analysis + mock-injection tests (10–30s, no API key needed).  
**Layer 2** — Claude API review for logic errors, security issues, and silent failures.

---

## Architecture

```
hook_validator.py          ← dispatcher (lives in your project root)
    │
    ├── validator_agent.py    ← Layer 1: static analysis + mock-injection tests
    └── claude_validator.py   ← Layer 2: Claude API review
         ↑
    config.json               ← project config + validator_mode
```

`hook_validator.py` reads `validator_mode` from `config.json` and decides which layers to run.

---

## Modes

Set `validator_mode` in `config.json`:

| Mode | Behaviour |
|---|---|
| `standalone` | Layer 1 only. Fast, no API key needed. |
| `claude-only` | Layer 2 only. Requires `ANTHROPIC_API_KEY`. |
| `cascade` | Layer 1 first → Layer 2 **only on full PASS**. |

### Behaviour without an API key

| Mode | No API key |
|---|---|
| `standalone` | Works normally — no key needed |
| `cascade` | Layer 1 runs; Layer 2 skipped with notice on stderr |
| `claude-only` | Hard stop with clear error — silent fallback would be misleading |

---

## Output

Both validators write to **stderr**, visible as a notification in the Claude Code UI after every file save:

```
[Validator] ✅ PASS (12/12) ~8s
[Claude]    ⚠️  1 issue: unsanitized input on line 47 (~22s)
```

Full reports are appended to `validation_log.md`.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | PASS |
| `1` | WARNINGS |
| `2` | FAIL / CRITICAL |

In cascade mode the exit code is the worst of the two layers.

---

## Setup

### 1. Copy `hook_validator.py` to your project root

```bash
cp /path/to/validation-agent/hook_validator.py /path/to/your/project/
```

### 2. Register the hook in `.claude/settings.json`

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/your/project/hook_validator.py"
          }
        ]
      }
    ]
  }
}
```

### 3. Configure `config.json`

```json
{
  "validator_mode": "cascade",

  "project_name": "your-project",
  "entry_point": "python3 main.py",
  "working_dir": "/path/to/your/project",

  "happy_path_tests": [
    {
      "description": "Normal flow works",
      "input": "1\nhello\nexit\n",
      "expected_output": "welcome"
    }
  ],

  "edge_case_template": "1\n{input}\nexit\n",
  "edge_case_inputs": [],
  "timeout_seconds": 8
}
```

### 4. Set your API key (cascade or claude-only)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 5. Install the Claude SDK (cascade or claude-only)

```bash
pip install anthropic
```

---

## What each layer catches

### Layer 1 — `validator_agent.py`
- Syntax errors
- Missing environment variables
- Dependency detection (network, database, external APIs, subprocess)
- Mock-injection unit tests for all public functions
- Per-file mode: 10–30 seconds
- Full-project mode: 2–5 minutes

### Layer 2 — `claude_validator.py`
Things static analysis cannot catch:
- **Intent vs implementation** — wrong conditions, inverted logic, off-by-one errors
- **Security with context** — SSRF, unsanitized user input, path traversal, hardcoded secrets
- **Silent failures** — swallowed exceptions, unchecked return values, error paths that succeed silently
- **Domain logic errors** — specific to your project's data flows and API contracts

---

## Running Layer 1 manually

Full project scan:

```bash
python validator_agent.py --project /path/to/your/project --config config.json
```

Per-file (same as hook mode):

```bash
CLAUDE_TOOL_INPUT_FILE_PATH=/path/to/file.py python validator_agent.py
```

### CLI flags

| Flag | Purpose |
|---|---|
| `--project PATH` | Project directory to test |
| `--config PATH` | Path to config.json |
| `--log PATH` | Override log file path |
| `--no-edge-cases` | Happy path tests only |
| `--category STRING` | Only edge cases matching STRING |

---

## config.json reference

| Field | Purpose |
|---|---|
| `validator_mode` | `standalone` \| `cascade` \| `claude-only` |
| `project_name` | Display name used in logs |
| `entry_point` | Shell command to run your project |
| `working_dir` | Working directory for test runs |
| `happy_path_tests[].input` | Stdin to feed the process (`\n` = Enter) |
| `happy_path_tests[].expected_output` | Comma-separated keywords; any match = pass |
| `edge_case_template` | `{input}` replaced with each edge case value |
| `edge_case_inputs` | Project-specific extra inputs |
| `timeout_seconds` | Per-test timeout in seconds |

### Designing `edge_case_template`

For a tool with a numbered menu where option 5 takes input:

```json
"edge_case_template": "5\n{input}\nexit\n"
```

---

## Built-in edge case categories

Layer 1 always tests these regardless of `config.json`:

- Type mismatches (int, bool, float, list, None)
- Empty string and whitespace-only (space, tab, newline)
- Unicode (CJK, Arabic, Cyrillic, emojis)
- Null bytes and control characters
- Very long inputs (100–1000 chars)
- Negative numbers and integer overflow
- Format errors (wrong separators, incomplete values)
- SQL injection, path traversal, shell injection, HTML injection

---

## Reading the log

`validation_log.md` is append-only. Each run adds a timestamped section:

```markdown
## [2026-04-06 14:32:01] phone_lookup.py — ✅ PASS (per-file, 8.3s)
- Functions found: 3
- Dependencies detected: requests, phonenumbers
- Tests: 6 passed, 0 failed

## [2026-04-06 14:32:24] phone_lookup.py — ✅ Claude PASS (22.1s)
- Issues: none
- Summary: Code handles all input paths correctly with appropriate error boundaries.
```

---

## File overview

| File | Location | Purpose |
|---|---|---|
| `hook_validator.py` | Your project root | Dispatcher: reads mode, runs validators, reports to stderr |
| `validator_agent.py` | `validation-agent/` | Layer 1: static analysis + mock-injection |
| `claude_validator.py` | `validation-agent/` | Layer 2: Claude API review |
| `config.json` | `validation-agent/` | Project config + `validator_mode` |
| `validation_log.md` | `validation-agent/` | Full audit log (append-only) |

---

## Requirements

- Python 3.10+
- `anthropic>=0.40.0` — required only for `cascade` and `claude-only` modes
- `ANTHROPIC_API_KEY` — required only for `cascade` and `claude-only` modes

Layer 1 uses only the Python standard library.
