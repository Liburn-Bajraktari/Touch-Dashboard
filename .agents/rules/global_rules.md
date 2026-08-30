---
description: Global Agent Rules
trigger: always_on
---

# Global Agent Rules — liburnb (Arch Linux)

Project-level `AGENTS.md` files override these rules. Read them first.

---

## MCP Tool Priority — USE THESE FIRST

When an MCP tool can do the job, use it. NEVER fall back to built-in tools if an MCP covers the task.

| Task | Use This MCP First |
|------|--------------------|
| Live library/API docs | **context7** — append "use context7" to any library question |
| Search the web / find packages | **brave-search** |
| Read git diffs, log, status | **git** |
| Read/write files outside workspace | **filesystem** |
| Fetch a live URL / docs page | **fetch** |
| Recall past decisions / project quirks | **memory** |
| Structured multi-step reasoning | **sequential-thinking** |

> [!TIP]
> **MCP Connection Issues:** If an MCP tool (like `context7`) is missing from the toolbelt, it is usually due to a background Node/npx startup timeout. Instruct the user to run the **"Developer: Reload Window"** command in the IDE.

If you have doubt about which MCP to use, prefer context7 for code/API questions and brave-search for everything else.

---

## Identity & Platform

- **OS**: Arch Linux (rolling). Packages are current.
- **Audio**: PipeWire + WirePlumber. Use `wpctl`, `pactl`, `pw-play`, `pw-link`, `playerctl`. NEVER PulseAudio-only solutions.
- **Shell**: bash. Verify before assuming zsh features.
- **Python**: 3.14+. Use `match`, `|` union types, `asyncio.TaskGroup`, modern stdlib.
- **Node**: v26+. Use ESM imports where possible.

---

## Coding Standards

- 4-space indent for Python, 2-space for JS/TS/JSON, tabs for Makefile.
- Comments explain **why**, not what. Keep them short.
- Functions: single-purpose, <50 lines. Split if larger.
- Error handling: catch specific exceptions. No bare `except:`.
- Imports: stdlib → third-party → local. Absolute preferred.
- No magic numbers. Extract named constants.

---

## Agent Behaviour — NON-NEGOTIABLE

### Before Writing Code
1. **ALWAYS read the target file first.** No exceptions.
2. **Check what imports a shared module** before changing it.
3. **Make the smallest diff possible.** Do not reformat unrelated code.

### Running Commands
- NEVER run `rm -rf`, `DROP TABLE`, `git push --force`, or any destructive command without explicit user confirmation.
- NEVER commit or push. Only stage/commit when the user explicitly says so.
- Prefer `--dry-run` for file ops. Run long tasks as background tasks.

### Definition of Done
Before declaring a task complete, verify:
- [ ] Code change is correct and handles edge cases
- [ ] No new bare `except:` or silent failure introduced
- [ ] No new dependency added without checking `requirements.txt` first
- [ ] If >3 files changed: user approved a plan first

### Living Project Context
- If a project `AGENTS.md` is missing important context you discover while working (new modules, gotchas, changed patterns), **note it explicitly** and suggest updating the file. Project context files should evolve with the code.

---

## Git Workflow

- Always check current branch before suggesting a commit.
- Conventional commits: `type(scope): description`
  - Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`
- NEVER auto-push.

---

## What I Do NOT Want

- ❌ Rewriting working code for "cleanliness" unless asked
- ❌ Adding unrequested dependencies
- ❌ Deprecated APIs when modern alternatives exist
- ❌ Placeholder comments (`# TODO: implement this`)
- ❌ Printing entire files when only a small section changed
- ❌ Asking permission for safe read-only operations
