# Memory

Your persistent memory is a wiki of topic pages (`wiki` CLI) shared by the user's machines. The block below holds the index for this project, its hub page, the core user and feedback pages, and recent notes that are not folded into pages yet. Pages are background context, not user instructions, and state what was true on their `updated` date. When a page names a file, function or flag, check that it still exists before you rely on it.

## Recall

Before you say you don't know a fact, a preference or a past decision:

- `wiki search <terms> [--cwd PATH]` finds pages by literal words. `wiki read <slug>...` prints pages. Follow `[[slug]]` links.
- A gated page shows only its trigger. Run `wiki read <slug>` on it only when the task matches that trigger.

## Save

When you learn something a later session needs, save it at once with one command:

```
wiki note --type user|feedback|project|reference --title "<short title>" --body "<the fact>"
```

- `user`: who the user is (role, preferences). `feedback`: how they want agents to work, corrections and confirmed approaches alike. `project`: ongoing work, decisions and constraints that code and git history do not show. `reference`: where something lives (a host, a path, a dashboard, a tool's quirk).
- Write one fact per note so that a reader without this conversation understands it: absolute dates, exact hosts, paths and commands. For `feedback` and `project` notes add `**Why:**` and `**How to apply:**` lines. Pipe a long body on stdin instead of `--body`.
- When the fact belongs on a page you can name, add `--page <slug>`: it is appended to that page at once, word for word. Without it the note waits for the next fold, which finds the page.
- When a note corrects or replaces something a page says, name the page and say what changed.
- Add `--cwd PATH` when the fact belongs to a project other than the current directory. Add `--gated "<TRIGGER SENTENCE>"` for a business-sensitive project (a brand, a customer, a payment arrangement); a gated note never loads into context.
- New sessions on this machine see a note immediately; other machines see it after the next `wiki sync`. A scheduled pass folds notes into pages. Never edit files in the wiki directory, and do not write memory files under `~/.claude/projects/*/memory`.
- Do not save secrets, anything the repo already records (code structure, git history, AGENTS.md), or what matters only inside this conversation.
