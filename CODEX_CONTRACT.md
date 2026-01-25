# CODEX_CONTRACT.md

GOAL:
Fully automate Indeed Easy Apply using Playwright CDP.

NON-NEGOTIABLE BEHAVIOR:
- Never remove existing functionality
- Never reduce waits unless explicitly instructed
- Never replace explicit error reasons with generic ones
- Every failure MUST be categorized:
  - applied
  - external
  - blocked (captcha / verification)
  - dead (404 / removed)
  - failed (with reason)

MANDATORY FEATURES:
- Detect and mark external ATS redirects immediately
- Detect captcha / “I’m not a robot” / verification blocks
- Retry and wait ≥ 8 seconds when no actionable button is found
- Always default-select first radio option if unanswered
- Resume screen:
  - Select “Use your Indeed Resume”
  - Click Continue even if offscreen / delayed
- Voluntary self-ID:
  - Gender: Male
  - Ethnicity: Not Hispanic/Latino
  - Race: White
  - Veteran: No
  - Disability: No, have not had one
  - Name: Andrew Pennington
  - Date: Today
- “How many years” → enter 15
- Final submit step must wait explicitly for submit button

DATABASE REQUIREMENTS:
- All outcomes MUST update DB
- External / blocked / dead jobs MUST NOT requeue
- Failed jobs must store explicit last_error

OUTPUT REQUIREMENTS:
- easy_apply_single.py
- easy_apply_batch.py
- No pseudocode
- No missing imports
- Must run without modifying environment
