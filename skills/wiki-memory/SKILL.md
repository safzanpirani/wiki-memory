---
name: wiki-memory
description: Long-term memory for coding agents, kept as a wiki of linked topic pages and driven by the `wiki` command. Use when the user says "remember this", "save that", "update memory" or "what do you know about X", when you are about to say you do not know a fact, preference or past decision, when the user asks to set up, install, check or repair wiki-memory, and when they want to see, graph, undo or tidy what the agent remembers.
---

# wiki-memory

The user's agents share one memory: a git repository of topic pages that link to each other. The `wiki` command reads and writes it. A session-start hook loads the pages for the current project, and you add facts with `wiki note`.

Run `wiki doctor` first when you are unsure of the state. It prints one line per check and the command that fixes each failing one.

## Set up

When `wiki` is not on the PATH:

```sh
curl -fsSL https://raw.githubusercontent.com/safzanpirani/wiki-memory/main/install.sh | sh
```

The script clones the repository to `~/.local/share/wiki-memory/src`, links `wiki` into `~/.local/bin`, creates an empty wiki, and wires every agent it finds on the machine: Claude Code, Codex, Droid, OpenCode, Pi, omp and Antigravity. It needs `python3` (3.9 or newer) and `git`. From a checkout, `./install.sh` does the same.

After it finishes:

1. Run `wiki doctor` and act on every `fix:` line the user wants.
2. Tell the user to start a new session. Hooks load when an agent starts, so the session you are in does not have the memory yet.
3. Offer the first pages. Both commands below call a model and cost money, so show the dry run and wait for a yes:
   - `wiki import --all --dry-run` when the user has Claude Code memory files. It prints the plan and a cost estimate.
   - `wiki bootstrap --dry-run` when they have none. It builds first pages from recent session history.
4. Offer `wiki install schedule`, which folds saved notes into pages every night.

`wiki install <target>` wires one agent: `claude-code`, `codex`, `droid`, `opencode`, `pi`, `omp` or `agy`. `wiki install --dry-run` lists what a bare `wiki install` would touch. `wiki install --remove` takes every hook and this skill out again.

## Recall

Before you say you do not know something:

```sh
wiki search <terms>            # ranked literal search; add --cwd PATH to stay in one project
wiki read <slug>...            # print pages; follow [[slug]] links
wiki backlinks <slug>          # pages that link to a page
wiki context --cwd <repo>      # what a session in another repo would load
```

A gated page shows only its trigger sentence. Read it with `wiki read <slug>` only when the task matches that trigger.

Pages state what was true on their `updated` date. When a page names a file, a function or a flag, check that it still exists before you rely on it.

## Save

Save a fact as soon as you learn it:

```sh
wiki note --type user|feedback|project|reference --title "<short title>" --body "<the fact>"
```

- `user` is who the user is. `feedback` is how they want agents to work. `project` is ongoing work and decisions that code and git history do not show. `reference` is where something lives.
- Write one fact per note, with absolute dates and exact hosts, paths and commands, so a reader without this conversation understands it.
- Add `--page <slug>` when you know the page. The text lands on that page at once, word for word. Without it the note waits in the inbox for the next fold.
- Add `--cwd PATH` when the fact belongs to another project.
- Add `--gated "<trigger sentence>"` for business-sensitive facts. A gated note never loads into a session.
- Do not save secrets, what the repository already records, or what matters only in this conversation.
- Do not edit files in the wiki directory, and do not write memory files under `~/.claude/projects/*/memory`.

## Look after it

| Task | Command |
|---|---|
| Health of the install | `wiki doctor` |
| Page counts, last passes, pending notes | `wiki status`, `wiki notes --all` |
| Draw the pages and their links | `wiki graph --open`; add `--anon` for a picture that is safe to post |
| Check links, orphans and frontmatter | `wiki lint`, then `wiki lint --fix` |
| What changed, and take it back | `wiki log`, `wiki history <slug>`, `wiki diff <ref>`, `wiki undo <ref> --dry-run` |
| Fold inbox notes into pages now | `wiki fold --dry-run`, then `wiki fold` (calls a model) |
| Share between machines | `wiki sync` |
| Settings | `wiki config` |

Every command takes `--json`. `wiki --help` lists every flag.

Model passes (`import`, `bootstrap`, `fold`, `dream`, `nightly`) run a headless agent in a sandbox and spend the user's quota. Run one only when the user asks for it, and never two at once: the second exits 4 because the first holds the lock. Exit code 5 means a provider limit stopped the run. Rerun the same command after the time it prints.
