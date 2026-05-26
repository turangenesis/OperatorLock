# Open PR Playbook

Use this when I say: “open PR”.

## Rules

- Never push directly to `main`.
- Never merge the PR.
- Never commit secrets, `.env`, API keys, tokens, credentials, or private config.
- Keep the PR focused on the current change only.
- If tests/build commands are unclear, inspect package files and docs first.
- If something is risky or ambiguous, stop and ask.

## Steps

1. Check current state:
   - `git status`
   - `git branch --show-current`
   - `git remote -v`

2. If currently on `main`, create a new branch:
   - Use format: `type/scope-short-description`
   - Examples:
     - `refactor/repo-docs-cleanup`
     - `feature/operator-lock-ui`
     - `fix/env-secret-handling`

3. Review changed files:
   - Summarize what changed.
   - Check for unrelated edits.
   - Check for secrets or sensitive files.

4. Run available checks:
   - Install dependencies only if needed.
   - Run tests if available.
   - Run lint/build if available.
   - If no tests exist, state that clearly.

5. Commit changes:
   - Use a clear conventional-style commit message.
   - Examples:
     - `refactor: clean portfolio project structure`
     - `docs: add execution platform architecture`
     - `fix: protect environment configuration`

6. Push branch:
   - `git push -u origin <branch-name>`

7. Open PR:
   - Use GitHub CLI if available:
     - `gh pr create`
   - PR title should be clear.
   - PR body should include:
     - Summary
     - Changes made
     - Tests/checks run
     - Risks/notes

8. Stop after PR creation:
   - Return PR link.
   - Do not merge.