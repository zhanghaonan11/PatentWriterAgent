# Repository Guidelines

## Project Structure & Module Organization
- Core workflow instructions live in `CLAUDE.md` and `.claude/agents/*.md` (8 staged subagents from parsing to merge).
- Reusable packaged skill content is under `patent-writer/`:
  - `patent-writer/SKILL.md`
  - `patent-writer/references/patent-writing-guide.md`
  - `patent-writer/references/agents/*.md`
- Sample input/output assets: `data/` (for example disclosure docs) and `output/` (generated artifacts, logs, stage folders).
- Root docs (`README.md`, `APP_README.md`, `PATENT_SKILL.md`, `arch.md`) define behavior and constraints; treat them as source-of-truth specs.

## Build, Test, and Development Commands
- `pip install -r requirements.txt`: install local Python dependencies (doc conversion + Streamlit deps).
- `claude --dangerously-skip-permissions "根据 data/输入.docx 编写专利提案" -p --output-format stream-json --verbose`: run the end-to-end patent generation pipeline.
- `docker build -t patent-writer .`: build the containerized runtime.
- `docker run -p 8009:8009 patent-writer`: run the container (Streamlit port exposed at `8009`).

## Coding Style & Naming Conventions
- Use clear, task-oriented Markdown with concise headings and ordered workflow steps.
- Keep agent and skill filenames in kebab-case (for example, `outline-generator.md`).
- Follow staged output naming exactly: `output/temp_[uuid]/01_input` ... `06_final`.
- JSON examples/configs should use 2-space indentation and stable key naming.

## Testing Guidelines
- There is no dedicated automated test suite in this repository today.
- Validate changes by running one full pipeline and checking required outputs:
  - `01_input/parsed_info.json`
  - `04_content/{abstract,claims,description}.md`
  - `06_final/complete_patent.md`
- For behavior changes, include a short manual verification note in the PR.

## Commit & Pull Request Guidelines
- Prefer Conventional Commit style seen in history (for example, `feat: ...`, `fix: ...`, `docs: ...`).
- Keep commits scoped to one concern (agent prompt update, skill packaging, docs correction, etc.).
- PRs should include: purpose, files changed, validation steps, and sample output path when relevant.
- Do not commit local secrets or machine-local config: `.mcp.json`, `.claude/settings.local.json`, and generated `output/` contents.

## Security & Configuration Tips
- Start from templates: `.mcp.json.example` and `.claude/settings.local.json.example`.
- Store API keys via local env/config only; never hardcode credentials in agent or skill files.
