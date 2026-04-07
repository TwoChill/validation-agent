# validation-agent — Claude Code Context

## What this project is

A Claude Code hook that automatically validates Python files after every Edit/Write. Runs static analysis always; optionally uses the Claude API for a second-opinion review and auto-fix agent.

## Claude API is optional

The `anthropic` package and `ANTHROPIC_API_KEY` are **not required**. The tool degrades gracefully:

- No API key → static analysis only (syntax, imports, mock-injection tests, edge cases)
- API key set → adds Claude review layer + auto-fix agent

Both `validator.py` and `validator_agent.py` check for the key at runtime and fall back silently if absent. Scoring still works — `SKIPPED` earns the same AI bonus as `PASS`.

## Key files

| File | Purpose |
|---|---|
| `hook_validator.py` | PostToolUse dispatcher — copied to user's project root |
| `validator.py` | Static analysis engine + optional Claude review layer |
| `validator_agent.py` | Tool-use agent loop that reads, fixes, and re-checks files |
| `install.sh` | Linux/macOS installer — clones repo, registers hook via safe JSON merge |
| `config.json` | Auto-generated per-install settings (never overwritten if exists) |
| `validation_log.md` | Append-only Claude review audit log |

## settings.json behavior

The installer **merges** into the user's existing `settings.json` — it never overwrites. Uses `setdefault` + duplicate checks before appending. The Windows README instructions use the same safe-merge Python script for consistency.

## Commit style

No co-author lines. Short imperative subject line, bullet body for non-trivial changes.
