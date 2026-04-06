# Validation Log

Persistent append-only record of all validation runs.
Never manually edited — managed by `validator_agent.py`.

---

*No runs recorded yet. Run `python validator_agent.py --project <path> --config config.json` to create the first entry.*

---
## [2026-04-04 13:31:21] osint-tool — ❌ CRITICAL
Config: `/root/agents/validation-agent/config.json`

### Happy Path Tests
- ✅ PASS **Happy Path #1: Main menu loads and exit works**
- ❌ CRASH **Happy Path #2: Invalid menu choice shows error, doesn't crash**
  - Error: `KeyError: 99`
  - Fix: Missing key '99' — use dict.get('99') or check key existence first
- ✅ PASS **Happy Path #3: Phone lookup with valid Dutch number (no API key, no export)**
- ✅ PASS **Happy Path #4: Non-numeric menu input falls through gracefully**

### Edge Case Tests


### Auto-Fix Suggestions

- Missing key '99' — use dict.get('99') or check key existence first

### Summary
- Happy path: 3/4 passed
- Edge cases: 0/0 handled
- **Overall: ❌ CRITICAL**


---
## [2026-04-04 15:24:28] osint-tool — ⚠️ WARNINGS

### Project Analysis
- Files scanned: 7
- Functions analyzed: 38
- Dependencies: requests, subprocess, open()

### Mocking Summary
- network (requests)
- subprocess
- file I/O → /tmp/validation_0dhp_svr
- env vars (HUDSONROCK_API_KEY)

### Test Execution
- Existing tests: none found
- **Generated smoke tests**: 0/0 passed  (0 failed, 0 errors) in 0.04s

### Suggestions
- No unit tests found — add pytest tests to a tests/ directory
- Missing env vars (mocked): HUDSONROCK_API_KEY — add to .env.example
- subprocess calls detected — validate inputs to prevent shell injection

### Summary
- Total passed: 0
- Total failed: 0
- **Overall: ⚠️ WARNINGS**


---
## [2026-04-04 15:25:10] osint-tool — ⚠️ WARNINGS

### Project Analysis
- Files scanned: 7
- Functions analyzed: 38
- Dependencies: requests, subprocess, open()

### Mocking Summary
- network (requests)
- subprocess
- file I/O → /tmp/validation_ipyclsjt
- env vars (HUDSONROCK_API_KEY)

### Test Execution
- Existing tests: none found
- **Generated smoke tests**: 57/61 passed  (4 failed, 0 errors) in 3.78s
  - Failures:
    - `../../tmp/validation_ipyclsjt/test_validation_smoke.py::test_main__importable`
    - `../../tmp/validation_ipyclsjt/test_validation_smoke.py::test_main__check_dependencies__none`
    - `../../tmp/validation_ipyclsjt/test_validation_smoke.py::test_main__check_dependencies__empty`
    - `../../tmp/validation_ipyclsjt/test_validation_smoke.py::test_main__check_dependencies__zero`

### Suggestions
- No unit tests found — add pytest tests to a tests/ directory
- Missing env vars (mocked): HUDSONROCK_API_KEY — add to .env.example
- subprocess calls detected — validate inputs to prevent shell injection

### Summary
- Total passed: 57
- Total failed: 4
- **Overall: ⚠️ WARNINGS**


---
## [2026-04-04 17:28:11] email_check.py — ❌ FAIL (per-file, 0.5s)

- File: `/root/osint-tool/email_check.py`
- Functions found: 10
- Dependencies detected: requests, open()
- Tests: 17 passed, 4 failed
- Failures:
  - `fetch_email(none): EOFError: EOF when reading a line`
  - `fetch_email(empty): EOFError: EOF when reading a line`
  - `run(none): EOFError: EOF when reading a line`
  - `run(empty): EOFError: EOF when reading a line`


---
## [2026-04-04 17:28:20] osint-tool — ⚠️ WARNINGS (full, pytest)

### Project Analysis
- Files scanned: 7
- Functions analyzed: 38
- Dependencies: requests, subprocess, open()

### Mocking Summary
- network (requests)
- subprocess
- file I/O → /tmp/validation_v003t_et
- env vars (HUDSONROCK_API_KEY)

### Test Execution
- Existing tests: none found
- **Smoke tests (pytest)**: 57/61 passed  (4 failed, 0 errors) in 3.19s
  - Failures:
    - `../../tmp/validation_v003t_et/test_validation_smoke.py::test_main__importable`
    - `../../tmp/validation_v003t_et/test_validation_smoke.py::test_main__check_dependencies__none`
    - `../../tmp/validation_v003t_et/test_validation_smoke.py::test_main__check_dependencies__empty`
    - `../../tmp/validation_v003t_et/test_validation_smoke.py::test_main__check_dependencies__zero`

### Suggestions
- No unit tests found — add pytest tests to a tests/ directory
- Missing env vars (mocked): HUDSONROCK_API_KEY — add to .env.example
- subprocess calls detected — validate inputs to prevent shell injection

### Summary
- Total passed: 57
- Total failed: 4
- **Overall: ⚠️ WARNINGS**


---
## [2026-04-04 17:28:43] osint-tool — ⚠️ WARNINGS (full, pytest)

### Project Analysis
- Files scanned: 7
- Functions analyzed: 38
- Dependencies: requests, subprocess, open()

### Mocking Summary
- network (requests)
- subprocess
- file I/O → /tmp/validation_th9ioqd1
- env vars (HUDSONROCK_API_KEY)

### Test Execution
- Existing tests: none found
- **Smoke tests (pytest)**: 57/61 passed  (4 failed, 0 errors) in 3.7s
  - Failures:
    - `../../tmp/validation_th9ioqd1/test_validation_smoke.py::test_main__importable`
    - `../../tmp/validation_th9ioqd1/test_validation_smoke.py::test_main__check_dependencies__none`
    - `../../tmp/validation_th9ioqd1/test_validation_smoke.py::test_main__check_dependencies__empty`
    - `../../tmp/validation_th9ioqd1/test_validation_smoke.py::test_main__check_dependencies__zero`

### Suggestions
- No unit tests found — add pytest tests to a tests/ directory
- Missing env vars (mocked): HUDSONROCK_API_KEY — add to .env.example
- subprocess calls detected — validate inputs to prevent shell injection

### Summary
- Total passed: 57
- Total failed: 4
- **Overall: ⚠️ WARNINGS**

