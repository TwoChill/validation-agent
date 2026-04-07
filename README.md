# validation-agent

Automatic code checker for Claude Code. Checks your files after every edit — fixes issues automatically if you add an API key.

**Works instantly. No setup needed. Claude API is optional.**

---

## 🚀 Quick Start (2 minutes)

### Linux / macOS

**Step 1 — Go to your project folder**

```bash
cd /path/to/your/project
```

**Step 2 — Install**

```bash
curl -sSL https://raw.githubusercontent.com/TwoChill/validation-agent/main/install.sh | bash
```

**Step 3 — Open Claude Code**

That's it. The checker runs automatically after every file edit.

**Want AI-powered auto-fix?** *(Optional)*

Get a free API key at [console.anthropic.com](https://console.anthropic.com), then run:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

To make it permanent, add that line to your `~/.bashrc` or `~/.zshrc`.

---

### Windows

**Step 1 — Open PowerShell and go to your project folder**

```powershell
cd C:\path\to\your\project
```

**Step 2 — Install (Git + Python required)**

```powershell
git clone https://github.com/TwoChill/validation-agent.git "$HOME\agents\validation-agent"
pip install anthropic  # optional — only needed for AI review and auto-fix
```

**Step 3 — Register the hook**

```powershell
Copy-Item "$HOME\agents\validation-agent\hook_validator.py" ".\hook_validator.py"
python "$HOME\agents\validation-agent\validator.py" --init --project (Get-Location)
```

Then register the hook by running this in PowerShell (merges safely into your existing settings — nothing is overwritten):

```powershell
$script = @'
import json, pathlib

settings_path = pathlib.Path.home() / ".claude" / "settings.json"
install_dir   = str(pathlib.Path.home() / "agents" / "validation-agent")
project_dir   = str(pathlib.Path.cwd())

try:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}

settings.setdefault("permissions", {}).setdefault("allow", [])
for perm in [
    f"Bash(python {install_dir}/validator.py:*)",
    f"Bash(python {install_dir}/validator_agent.py:*)",
]:
    if perm not in settings["permissions"]["allow"]:
        settings["permissions"]["allow"].append(perm)

settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
already = any(
    any(h.get("command", "").endswith("hook_validator.py") for h in e.get("hooks", []))
    for e in settings["hooks"]["PostToolUse"]
)
if not already:
    settings["hooks"]["PostToolUse"].append({
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": f"python {project_dir}/hook_validator.py"}]
    })

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
print(f"Hook registered in {settings_path}")
'@
python -c $script
```

> This script reads your existing `settings.json`, adds only what's missing, and writes it back — your other settings are untouched.

**Step 4 — Open Claude Code**

The checker runs automatically after every file edit.

**Want AI-powered auto-fix?** *(Optional)*

Get a free API key at [console.anthropic.com](https://console.anthropic.com), then run:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

To make it permanent (current user, all sessions):

```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
```

---

## ✅ What This Does

- Checks your Python files for errors automatically after every edit in Claude Code
- Shows clear, plain-English results — no jargon
- Fixes issues automatically using AI when an API key is set *(optional)*

---

## ⚙️ Modes

You never need to choose a mode. It decides automatically:

| Situation | What happens |
|---|---|
| No API key | Checks code for errors, crashes, and bad inputs |
| API key set | Also reviews logic and security — fixes failures automatically |

---

## 🔑 API Key *(Optional)*

You do **not** need an API key to use this tool.

Without a key → checks syntax, imports, and basic function behavior.

With a key → also catches logic errors, security issues, and auto-fixes problems.

Get one free at [console.anthropic.com](https://console.anthropic.com).

> **Note:** A Claude Pro subscription (claude.ai) is separate from the API. You need an API key specifically — they have different billing.

---

## 📂 Project Setup

After install, your files look like this:

```
your-project/
├── hook_validator.py        ← added by install (don't edit)
└── .claude/
    └── settings.json        ← hook registered automatically

~/agents/validation-agent/          (Linux/macOS)
%USERPROFILE%\agents\validation-agent\   (Windows)
├── validator.py             ← the checker engine
├── validator_agent.py       ← the AI fixer
├── config.json              ← auto-generated settings
└── validation_log.md        ← full history of every check
```

You don't need to touch any of these files.

---

## 🧪 What You'll See

**All good:**
```
✅ Code is OK  [2.1s]
Score: 95/100
```

**Minor issue:**
```
⚠️  1 issue found:
   Line 47: user input is not validated before use
Score: 72/100
  Correctness 40/40  Security 15/20  Robustness 10/30  AI Review 7/10
```

**Serious problem:**
```
❌ Critical issue:
   What broke: SyntaxError — unexpected indent (line 12)
   Why it matters: The file cannot run at all
Score: 0/100
```

**Auto-fix in progress (if API key is set):**
```
[Auto-fix] Issue detected — running AI repair...
[Agent] Starting on app.py ...
[Agent]   → read_file(['path'])
[Agent]   → write_fix(['path', 'fixed_code'])
[Agent] ✅ FIXED — 2 round(s), 1,200↑ 400↓ tokens, ~$0.0480
Score: 98/100
```

---

## 🧠 Advanced

<details>
<summary>Architecture, internals, and configuration</summary>

### How it works

```
hook_validator.py        ← runs after every Edit/Write in Claude Code
    │
    ├── validator.py     ← static analysis + optional Claude review
    │       └── .validator_findings.json   ← handoff to agent
    │
    └── validator_agent.py  ← AI agent: reads, fixes, re-checks
```

### What gets checked

**Static analysis (always, no API key needed):**
- Syntax errors
- Import failures
- Missing environment variables
- Mock-injection tests for all public functions
- Edge case inputs: empty strings, unicode, SQL injection strings, path traversal, null bytes, very long input, type mismatches

**AI review (with API key):**
- Logic errors — wrong conditions, inverted checks, off-by-one
- Security issues in context — not just "requests is used" but specific risks
- Silent failures — swallowed exceptions, unchecked return values
- Domain-specific bugs based on your project's data flow

**Auto-fix agent (with API key, triggers on failure):**
1. Reads the file and findings from static analysis
2. Identifies the root cause
3. Writes a targeted fix
4. Re-runs validation to verify
5. Repeats until clean or limit is hit

### Agent guardrails

| Limit | Value |
|---|---|
| Max fix iterations | 8 |
| Wall-clock timeout | 120 seconds |
| Repeated fix detection | Stops if same fix appears twice |
| No-improvement detection | Stops if errors don't decrease |

### Scoring system

Every check gives you a score out of 100:

| Category | Points | How it's measured |
|---|---|---|
| Correctness | 40 | Ratio of passing mock tests |
| Security | 20 | Deducted for each flag (strict mode) |
| Robustness | 30 | Deducted for each failing edge case |
| AI Review | 10 | Full points if AI says PASS or SKIPPED |

### Security strict mode

By default, security checks run but don't affect the score.
To make security issues count against the score, add this to `config.json`:

```json
"security_strict": true
```

This flags:
- Hardcoded secrets (passwords, tokens in code)
- Code injection risks (`eval`, `exec`, `shell=True`)
- Unsafe deserialization (`pickle.loads`)
- Unsafe file writes

### config.json reference

Auto-generated on first run. Edit if needed:

| Field | What it does | Default |
|---|---|---|
| `project_name` | Name shown in logs | folder name |
| `entry_point` | How to run your project | auto-detected |
| `working_dir` | Where to run it from | `.` |
| `timeout_seconds` | Max seconds per test | `8` |
| `security_strict` | Count security issues in score | `false` |

### Manual use

**Linux / macOS:**

Check a specific file right now:
```bash
CLAUDE_TOOL_INPUT_FILE_PATH=/path/to/file.py python3 ~/agents/validation-agent/validator.py
```

Scan an entire project:
```bash
python3 ~/agents/validation-agent/validator.py --project /path/to/your/project
```

Fix a file with AI:
```bash
python3 ~/agents/validation-agent/validator_agent.py /path/to/file.py
```

**Windows (PowerShell):**

Check a specific file right now:
```powershell
$env:CLAUDE_TOOL_INPUT_FILE_PATH = "C:\path\to\file.py"
python "$HOME\agents\validation-agent\validator.py"
```

Scan an entire project:
```powershell
python "$HOME\agents\validation-agent\validator.py" --project C:\path\to\your\project
```

Fix a file with AI:
```powershell
python "$HOME\agents\validation-agent\validator_agent.py" C:\path\to\file.py
```

### Log file

**Linux / macOS:** `~/agents/validation-agent/validation_log.md`

**Windows:** `%USERPROFILE%\agents\validation-agent\validation_log.md`

Append-only, timestamped.

### Requirements

- Python 3.10+
- `anthropic` Python package — only needed for AI features (`pip install anthropic`)
- `ANTHROPIC_API_KEY` — optional, only needed for AI review and auto-fix

### File overview

| File | Where | Purpose |
|---|---|---|
| `hook_validator.py` | Your project root | Dispatcher — runs after every file edit |
| `validator.py` | `validation-agent/` | Static analysis + optional Claude review |
| `validator_agent.py` | `validation-agent/` | AI agent that fixes code |
| `config.json` | `validation-agent/` | Settings (auto-generated) |
| `validation_log.md` | `validation-agent/` | Full audit log |

</details>
