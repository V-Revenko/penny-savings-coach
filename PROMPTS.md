# Prompts to paste into Claude Code, one at a time

Wait for each step to finish and check the result before sending the next.

1. Read CLAUDE.md. Set up a Python virtual environment and install requirements.txt. Then build Tier 1 engine only: app/data.py (reading the Excel file directly for now, outputting the canonical columns) and app/engine.py, plus pytest tests that reproduce the reference table. Run the tests and show me the results before building anything else.

2. Build app/validate.py and app/explain.py with the offline template fallback. Add tests for the validator, including one where a wrong dollar amount gets blocked. Load the API key from .env with python-dotenv. Show me one live AI message for MBR-0026 and the validator report.

3. Build the Tier 1 UI: FastAPI app/main.py and static/index.html (phone frame, no login). Include the member picker, the recommendation screen with Accept / Adjust / Not now, the "How we checked this" panel, and the corrupt-a-number demo toggle. Tell me how to run it.

4. Build Tier 2 from CLAUDE.md: replay mode starting 2026-06-01, the "Advance to next payday" button, in-app notifications, and planned-vs-saved history.

5. Build Tier 3: the "We found $X/month" card with Confirm / Not a duplicate.

6. (Database, if the teammate hasn't done it) Build scripts/schema.sql and scripts/build_db.py per the "Normalized database" section, including the quality gate and the views. Switch app/data.py to read the views, and confirm all tests still pass.

7. Generate a Mermaid erDiagram of the database schema for the presentation slide.

If time allows: Tier 4 (MBR-0081 hand-off screen, MBR-0055 irregular income).
