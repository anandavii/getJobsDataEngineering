# Plan 09: `push-commit-pr` Skill

## Context
Create a reusable Claude Code skill (`/push-commit-pr`) installed globally at `~/.claude/skills/push-commit-pr/SKILL.md`. The skill handles the full "I'm done with this work" flow in one command: audit → stage → commit (AI-generated message) → push → open PR.

---

## Skill Location
`~/.claude/skills/push-commit-pr/SKILL.md`

No bundled scripts — all operations use `git` and the `gh` CLI.

---

## Flow (in order)

### 0. Safety Gate
Before performing any action:

- Verify inside a valid Git repository
- Verify **not on `main` or `master`**
  - If on protected branch → refuse and instruct user to create a feature branch
- Verify **not in detached HEAD state**
- Verify working tree is not in a conflicted state
If any safety condition fails → inform user and stop.

### 1. Audit changes
- Run `git status` + `git diff --stat`
- If nothing to commit, inform user and stop

### 2. Stage
- Default: stage all changed/untracked files (`git add -A`)
- If user specifies files via `--files`, stage only those
- Show staged summary after

### 3. Generate commit message
- Read full `git diff --cached`
- Generate a conventional commit: `type(scope): short summary`
- Show proposed message to user — they can approve or override
- Commit with approved message

### 4. Push
- If branch has no upstream, set with `-u origin <branch>`
- If local is behind remote → warn and stop (never force-push)
- If on `main` or `master` → refuse with instructions to branch first
- Push to remote

### 5. Open PR
- Detect base branch: `main` → fallback `develop`
- Generate PR title from commit message(s)
- Generate PR body:
  - `## Summary` — bullet points from the diff
  - `## Test plan` — checkbox checklist of how to test
  - `🤖 Generated with Claude Code` footer
- Check for `.github/PULL_REQUEST_TEMPLATE.md`; if present, use as base structure
- Run `gh pr create --title "..." --body "..."`
- Return PR URL to user

---

## Supported Flags
| Flag | Effect |
|------|--------|
| `--draft` | Create a draft PR |
| `--message "..."` | Skip AI commit message, use this instead |
| `--base <branch>` | Override base branch for PR |
| `--files <f1> <f2>` | Stage only specific files |
| `--no-pr` | Commit + push only, skip PR creation |

---

## Trigger Phrases (for skill description)
- `/push-commit-pr`
- "commit and push", "push and open a PR", "commit push PR", "ship this"
- User on a branch says "open a PR" or "create a PR"

---

## Safety Rails
- Never push to `main`/`master`
- Never amend published commits
- Never use `--no-verify`
- Always confirm before committing/pushing
- If branch is behind remote: warn and stop

---

## Prerequisites
- `git` installed, repo initialized
- `gh` CLI installed and authenticated
- At least one remote configured

---

## Verification
1. In any git repo with changes, type `/push-commit-pr` — skill triggers
2. Commit message proposal is shown before committing
3. Pushing to `main` is refused
4. PR body contains Summary bullets + Test plan checklist
5. PR URL is returned at the end
