# wiki-memory

Long-term memory for coding agents, kept as a wiki. Agents write short notes while they work, a model pass files each note on the topic page where it belongs, and every new session starts with the pages for its project already loaded.

It follows Andrej Karpathy's ["LLM wiki"](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern: raw sources stay untouched, a model maintains interlinked Markdown pages, and a small CLI indexes, checks and versions them. `wiki` is one Python file with no dependencies beyond Python 3.9 and git.

```
session starts ──► wiki context ──► index + project hub + core pages + recent notes
agent learns   ──► wiki note    ──► inbox/  (one file, no model, no lock)
scheduled pass ──► wiki fold    ──► pages/  (a sandboxed model run, one git commit)
```

## Why a wiki

Claude Code's built-in memory stores one fact per file with an index per project. On the author's machines that grew to 1,583 files in 151 project folders, and 305 of those files mentioned the same server. Facts that span projects were copied into each project, and the copies drifted apart.

A wiki has one page per subject. The page about a server holds everything known about it, and each project's hub page links to it. After importing those 1,583 files the wiki had 695 pages. The block a session loads for one project is 16,000 characters: a scoped index, the project hub and the few pages marked `core`.

## Quick start

```sh
git clone https://github.com/safzanpirani/wiki-memory
cd wiki-memory
./install.sh claude-code      # links `wiki` into ~/.local/bin, creates the wiki, adds the session hook
wiki doctor                   # shows what is wired and what is missing
```

For omp, run `./install.sh omp`. For Codex or another agent, see [Other agents](#other-agents).

If you already have Claude Code memory, import it:

```sh
wiki import --all --dry-run   # the plan and a cost estimate; calls no model
wiki import --all             # compile the memory files into pages
wiki verify                   # how much exact detail made it across
```

If you have no memory files, start from your session history instead:

```sh
wiki bootstrap --dry-run      # which projects and how many sessions it would read; calls no model
wiki bootstrap                # one model run per project writes its first pages, then asks three questions
```

`wiki bootstrap` reads the newest 15 sessions of each project from the last 30 days (`--days`, `--max-sessions`, `--project`), skips excluded directories, and creates the two pages every session loads: a user profile and a working-style page. You can also skip it: with only the hook installed, pages appear as agents save notes.

Then schedule the unattended pass:

```sh
wiki install schedule         # every night at 04:30; --at HH:MM picks another time
```

The job is saved with your current `PATH` and `WIKI_*` settings. Exit codes of `wiki nightly`: 0 all steps ran, 4 a step found the lock held, 5 a step stopped on a provider limit, 1 a step failed.

```sh
wiki doctor
```

Start a new agent session in any project. It now opens with that project's memory, and it saves new facts with `wiki note`.

## What a session sees

`wiki context --cwd <project> --instructions` prints the block the hook injects:

1. How to recall and how to save (`prompts/agent.md`).
2. An index: this project's pages, pages of related projects, global rules, and the triggers of gated pages.
3. The project's hub page and the pages tagged `core`, in full.
4. Notes in scope that no fold has filed yet, newest first.

`--budget-chars N` is a hard cap on parts 2 to 4 (default 16000). The instructions add about 2,500 characters on top.

During the session the agent looks things up with `wiki search <terms>` and `wiki read <slug>`.

## Saving a fact

```sh
wiki note --type feedback --title "Keep replies short" --body "Lead with the result, under ten lines. **Why:** read on a phone."
wiki note --type project --page shop --title "Deploy flag" --body 'Prod deploys use `make ship ENV=prod`.'
wiki note --type project --gated "the Acme contract is mentioned" --body "..." --title "Acme pricing"
```

A plain note is a file in `inbox/`. Writing it takes no lock and no git command, so any number of sessions can save at the same moment, also while a model pass runs. New sessions see the note at once in the "recent notes" part of their context.

`--page <slug>` appends the fact to that page immediately, word for word, and commits. No model is involved. If a model pass holds the lock at that moment, the note waits in the inbox and carries the page name.

`--type` is `user` (who the user is), `feedback` (how they want agents to work), `project` (decisions and state that code and git history do not show) or `reference` (where something lives). `user` and `feedback` notes show in every project; the others show in their own project.

## Folding

`wiki fold` gives the inbox to a model. The model reads the index and the pages a note touches, puts each fact on the right page, and deletes the note. The CLI then normalizes frontmatter, regenerates the index, appends to `log.md` and makes one git commit. `wiki undo <sha>` reverts a pass as a new commit and leaves later edits alone.

The model never works on the live wiki. It gets a scratch copy; the CLI copies a page back only if the live page did not change in the meantime, and sends the rest through the inbox for a serial merge. That is how four import batches can run in parallel without overwriting each other.

## Importing Claude Code memory

`wiki import --all` reads `~/.claude/projects/*/memory/*.md`, splits the files into batches of about 220 KB, and compiles each batch into pages. Cross-project folders (your home directory and anything in `WIKI_GLOBAL_DIRS`) go first and serially; project batches follow in parallel (`--parallel N`, default 4).

The dry run prints an estimate. It is based on one measured import: 1,583 files (4.2 MB) took 58 model runs and 4 hours of run time on an Opus-class model, and 115 million tokens went through the model, 93% of them cached re-reads. That cost $63 at list prices. A smaller model costs a fraction of that, and a subscription runner draws on its quota. An import that stops on a rate limit exits 5 and prints when to retry; running the same command again continues where it stopped.

A batch counts as done when every one of its files is listed in the `sources` of some page. `wiki import --report` shows coverage. `wiki verify` checks content: it takes the code spans of 6 to 80 characters and the dates from each memory file and looks for them verbatim in the pages (files with fewer than four such details are skipped). On the author's main project that found 91% of 4,034 details, with a median of 96% per file, and it lists the weakest files so you can re-import them.

The memory files are never modified. `wiki import --refresh-all` re-imports the ones whose content changed since the wiki took them in, so agents that still write memory files keep feeding the wiki.

`wiki dream` reads recent Claude Code and omp session transcripts and updates pages from what happened in them. It is optional.

## Safety

- **Sandbox.** A model pass runs a headless agent with every tool approved, so it starts inside a sandbox: `sandbox-exec` on macOS, `bwrap` on Linux. The sandbox denies writes to the live wiki, the memory folders, this CLI's code and config, and the instruction, skill and settings files your agents load. Without a sandbox the pass refuses to start; `WIKI_SANDBOX=0` overrides that. The macOS profile also blocks renaming or unlinking the protected paths and their parent folders. The Linux one makes the protected paths read-only where they are and does not stop a run from renaming a parent folder, and it cannot protect a file or memory folder that does not exist yet. Treat it as a guard against a wandering agent, not a hostile one.
- **Gated pages.** A memory file whose name or description says `DO NOT READ UNLESS …`, and a note saved with `--gated`, becomes a gated page. The CLI copies it verbatim; no model reads it. In the index it appears as its trigger sentence only, it never loads into context, and `wiki search` skips it unless you pass `--gated`.
- **Redaction.** Token-shaped strings (API keys, GitHub tokens, private key blocks) are replaced with `[REDACTED-SECRET]` in notes before they are written, in the copies a model reads, in run logs, and in every file before it is committed.
- **Excluded projects.** Directories listed in `~/.config/wiki-memory/exclude.txt`, one per line, are never imported or dreamt over.
- **Undo.** The wiki is a git repo. Each pass is one commit, and `wiki history <slug>`, `wiki diff <ref>` and `wiki undo <ref>` work on it.

## How much automation

Nothing reads your session transcripts unless you run `wiki dream` or `wiki bootstrap` yourself, and nothing is scheduled until you run `wiki install schedule`. Three common setups:

| Setup | What to do |
|---|---|
| No model at all | Install the hook only. Agents save with `wiki note --page <slug>`, which appends to a page without a model. |
| Fold only | `wiki config set WIKI_NIGHTLY fold`, then `wiki install schedule`. A nightly run files the inbox notes and does nothing else. |
| Full | The default `WIKI_NIGHTLY=sync,refresh,fold`. Add `dream` to the list to also read each day's sessions. |

`wiki config` prints every setting, its value and whether it comes from the environment, the settings file or the default. `wiki config set KEY VALUE` writes the settings file. `WIKI_IMPORT_MODEL`, `WIKI_FOLD_MODEL` and `WIKI_DREAM_MODEL` pick a model per kind of pass.

`wiki install claude-code` (and `omp`, `codex`) also writes a short marked block into that agent's global instructions file (`~/.claude/CLAUDE.md`, `~/.omp/agent/AGENTS.md`, `~/.codex/AGENTS.md`). The hook loads the memory at session start; the block tells the agent when to search and when to save for the rest of the session. `--no-instructions` skips it and `--remove` takes it out again, leaving your own text as it was.

## Sharing between machines

Add a private git remote to the data repo and run `wiki sync` on each machine (the nightly pass does it too):

```sh
git -C ~/.local/share/wiki-memory/wiki remote add origin git@github.com:you/my-wiki.git
wiki sync
```

`wiki sync` commits pending notes, pulls with rebase and pushes. Let one machine own the pages: set `WIKI_NOTES_ONLY=1` on the others, and they only add notes and sync. Two machines then never edit the same file.

## The model runner

Model passes run through a headless agent you already have. `wiki` picks the first one it finds, or you set it:

| `WIKI_RUNNER` | Command |
|---|---|
| `claude` | `claude -p <prompt> --dangerously-skip-permissions` |
| `codex` | `codex exec --dangerously-bypass-approvals-and-sandbox -C <dir> <prompt>` |
| `omp` | `omp -p --approval-mode yolo --cwd <dir> <prompt>` |
| any | `WIKI_RUNNER_CMD="my-agent --dir {cwd} --model {model} {prompt}"` |

`WIKI_MODEL` is passed as `--model` when set. The permission flags are there because nobody is at the terminal; the sandbox described above is what limits the run.

## Other agents

Any agent that can run a command at session start can use the wiki. For Codex, add this to `~/.codex/AGENTS.md`:

```
At the start of each session run `wiki context --instructions` and treat its output as your memory.
```

## Settings

Environment variables, or the same `KEY=VALUE` lines in `~/.config/wiki-memory/env`. The environment wins.

| Variable | Default | Meaning |
|---|---|---|
| `WIKI_HOME` | `~/.local/share/wiki-memory/wiki` | the data repo: `pages/`, `inbox/`, `index.md`, `log.md`, `SCHEMA.md` |
| `WIKI_STATE` | `~/.local/share/wiki-memory/state` | lock, logs, scratch copies, the import ledger |
| `WIKI_PROJECTS` | `~/.claude/projects` | where Claude Code memory folders live |
| `WIKI_RUNNER`, `WIKI_RUNNER_CMD`, `WIKI_MODEL` | first of claude, codex, omp | the model runner |
| `WIKI_SANDBOX` | `1` | `0` runs model passes without a sandbox |
| `WIKI_NOTES_ONLY` | unset | `1`: this machine never runs a model pass |
| `WIKI_EXCLUDE` | `~/.config/wiki-memory/exclude.txt` | project directories to leave out |
| `WIKI_GLOBAL_DIRS` | unset | folders whose memory is about everything (a dotfiles or ops repo); imported first |
| `WIKI_UMBRELLA` | unset | parent folders that hold many repos; a page scoped to one does not apply to every repo below it |
| `WIKI_BATCH_CHARS`, `WIKI_MAX_TIME` | `220000`, `50m` | batch size and time limit of one model run |

## Commands

Every command takes `--json`. Exit codes: 0 ok, 1 failed, 2 usage, 3 lint issues or undo conflicts left, 4 another run holds the lock, 5 stopped on a provider limit.

| Command | Model | Does |
|---|---|---|
| `context`, `index`, `search`, `read`, `backlinks`, `log` | no | read the wiki |
| `note`, `notes` | no | save a fact; list notes not folded yet |
| `status`, `doctor`, `lint [--fix]`, `verify` | no | state, install check, page checks, import fidelity |
| `history`, `diff`, `undo` | no | git history per page; revert a pass |
| `sync` | no | commit notes, pull, push |
| `install bin\|claude-code\|omp\|schedule [--remove]` | no | wire the CLI into this machine |
| `import`, `fold`, `dream`, `nightly` | yes | compile memory files; fold notes; read sessions; the scheduled pass |

`wiki --help` and the top of `wiki.py` list every flag.

## Limits

- Search is literal. There is no embedding index; the loaded index and `wiki search` are the recall path.
- A model rewrites pages, so a fold can drop or blur a detail. `wiki verify`, `wiki diff` and `--page` notes exist for that reason.
- The token estimate comes from one import with one model. Yours will differ.
- Token and cost totals in `wiki status` are known only with the omp runner.
- macOS and Linux. Windows is not supported.
- Claude Code keeps its own memory files as well. The hook tells the agent to save with `wiki note`; `wiki import --refresh-all` picks up anything it still writes to those files.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests build a temporary home, wiki and memory tree and use `tests/fake_omp.py` in place of a model. They cover planning, parallel batches and merging, resume, the ledger, gating, redaction, the macOS sandbox, notes, folding, sync between two clones, the installers and the nightly pass.

## License

MIT
