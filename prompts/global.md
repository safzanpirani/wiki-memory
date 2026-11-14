## Memory (wiki-memory)

Your long-term memory is a wiki managed by the `wiki` command. It replaces any built-in memory files.

- **Recall first.** Before you say you do not know a fact, a preference or a past decision, run `wiki search <terms>` and open hits with `wiki read <slug>`. In a repo other than the one the session started in, run `wiki context --cwd <repo>`.
- **Save as you go.** When the user corrects you, states a preference, makes a decision or you learn where something lives, save it at once: `wiki note --type user|feedback|project|reference --title "<short title>" --body "<the fact>"`. One fact per note, with absolute dates and exact paths and commands. Add `--page <slug>` when you know the page it belongs on.
- **"Remember this", "update memory" and "save that"** mean `wiki note`. Do not write memory files or edit files in the wiki directory.
- **Gated pages** show only a trigger sentence. Read one only when the task matches its trigger. Save business-sensitive facts with `--gated "<trigger>"`.
- Never save secrets, and do not save what the repo already records.
