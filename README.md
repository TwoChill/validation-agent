# Validation Agent

A two-layer code validator that hooks into Claude Code's `PostToolUse` event. After every file edit, it automatically validates the changed file and reports directly in the Claude Code UI.

**Layer 1 — `validator.py`** Fast static analysis + mock-injection tests. No API key needed. Includes an optional Claude review pass if `ANTHROPIC_API_KEY` is set (silently skipped if not).

**Layer 2 — `validator_agent.py`** A real AI agent. Claude drives a tool-use loop to read, fix, and verify code. Runs only when you switch to `full` or `agent` mode.

---

## Architecture

```
hook_validator.py           ← dispatcher (lives in your project root)
    │
    ├── validator.py         ← Layer 1: static analysis + optional Claude review
    │        │
    │        └── .validator_findings.json   ← findings handoff file (auto-created)
    │
    └── validator_agent.py   ← Layer 2: real AI agent (Claude tool-use loop)
         ↑
    config.json              ← project config + validator_mode
```

`hook_validator.py` reads `validator_mode` from `config.json` and decides which layers to run.

---

## Modes

Set `validator_mode` in `config.json`:

| Mode | Behaviour |
|---|---|
| `static` | Layer 1 only. Fast, no API key needed. Claude review runs silently if key is present. |
| `full` | Layer 1 first. If PASS and API key present, Layer 2 (agent) runs with findings as context. |
| `agent` | Layer 2 directly. If no API key: one message printed, then falls back to Layer 1. |

### No API key behaviour

| Mode | No API key |
|---|---|
| `static` | Works fully — Claude review silently skipped, no message |
| `full` | Layer 1 runs and completes; Layer 2 silently skipped |
| `agent` | Prints one message, then runs Layer 1 as fallback |

---

## Output

Validators write to **stderr**, visible as notifications in the Claude Code UI after every file save:

```
[Validator] phone_lookup.py ...
✅ PASS — phone_lookup.py  (6/6 passed)  [8.3s]
[Claude]    ⚠️  WARNINGS — 1 issue: [line 47] unsanitized user input (~22s)

[Agent]  → read_file(['path'])
[Agent]  → run_code(['path'])
[Agent]  → write_fix(['path', 'fixed_code'])
[Agent] ✅ FIXED — 3 round(s), 2,100↑ 800↓ tokens, ~$0.0915
```

Full reports are appended to `validation_log.md`.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | PASS / FIXED |
| `1` | WARNINGS / PARTIAL |
| `2` | FAIL / CRITICAL |

In `cascade` mode the exit code is the worst of both layers.

---

## Setup

### 1. Clone this repo

```bash
git clone https://github.com/TwoChill/validation-agent.git /root/agents/validation-agent
```

### 2. Copy `hook_validator.py` to your project root

```bash
cp /root/agents/validation-agent/hook_validator.py /path/to/your/project/
```

### 3. Register the hook in `.claude/settings.json`

```json
{
  "permissions": {
    "allow": [
      "Bash(python3 /root/agents/validation-agent/validator.py:*)",
      "Bash(python3 /root/agents/validation-agent/validator_agent.py:*)"
    ]
  },
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

### 4. Configure `config.json`

```json
{
  "validator_mode": "static",

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

### 5. Install the Anthropic SDK (for Layer 2)

```bash
pip install anthropic
```

### 6. Set your API key (for Layer 2)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

> **Claude Pro ≠ API access.**
> A Claude Pro subscription (claude.ai) and the Anthropic API are separate services with separate billing.
> Even with an active Pro subscription, you need an API key for Layer 2.
> Create one at [console.anthropic.com](https://console.anthropic.com).

---

## What each layer catches

### Layer 1 — `validator.py` (static + Claude review)

Static analysis (no API key needed):
- Syntax errors
- Missing environment variables
- Dependency detection (network, database, external APIs, subprocess)
- Mock-injection unit tests for all public functions

Claude review (API key optional — silently skipped if absent):
- **Intent vs implementation** — wrong conditions, inverted logic, off-by-one errors
- **Security with context** — SSRF, unsanitized user input, path traversal, hardcoded secrets
- **Silent failures** — swallowed exceptions, unchecked return values, error paths that succeed silently
- **Domain logic errors** — specific to your project's data flows and API contracts

### Layer 2 — `validator_agent.py` (real agent)

Claude drives a tool loop. It decides what to do next based on what it observes:

1. Reads the file
2. Identifies bugs from validator findings + its own analysis
3. Writes a fix
4. Runs the file to verify
5. Repeats until the code is clean or the limit is hit

**Guardrails:**
- Max 8 tool-call iterations per run
- 120-second wall-clock timeout
- Token usage and cost estimate printed after every run

---

## Running manually

### Layer 1 — per-file (same as hook mode)

```bash
CLAUDE_TOOL_INPUT_FILE_PATH=/path/to/file.py python3 validator.py
```

### Layer 1 — full project scan

```bash
python3 validator.py --project /path/to/your/project
```

### Layer 2 — fix a specific file

```bash
python3 validator_agent.py /path/to/file.py
```

### Layer 1 CLI flags

| Flag | Purpose |
|---|---|
| `--project PATH` | Project directory to scan |
| `--log PATH` | Override log file path |

---

## config.json reference

| Field | Purpose |
|---|---|
| `validator_mode` | `static` \| `full` \| `agent` |
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

## [2026-04-06 14:33:10] phone_lookup.py — ✅ Agent FIXED
- Iterations: 3/8
- Tokens: 2,100 input / 800 output
- Estimated cost: ~$0.0915
```

---

## File overview

| File | Location | Purpose |
|---|---|---|
| `hook_validator.py` | Your project root | Dispatcher: reads mode, runs layers, reports to stderr |
| `validator.py` | `validation-agent/` | Layer 1: static analysis + optional Claude review |
| `validator_agent.py` | `validation-agent/` | Layer 2: real AI agent with tool-use loop |
| `config.json` | `validation-agent/` | Project config + `validator_mode` |
| `validation_log.md` | `validation-agent/` | Full audit log (append-only) |
| `.validator_findings.json` | `validation-agent/` | Auto-created handoff between Layer 1 and Layer 2 |

---

## Requirements

- Python 3.10+
- `anthropic>=0.40.0` — required only for `cascade` and `agent-only` modes
- `ANTHROPIC_API_KEY` — required only for `cascade` and `agent-only` modes

Layer 1 static analysis uses only the Python standard library.
