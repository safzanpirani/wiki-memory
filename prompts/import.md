You are compiling the user's agent memory into a wiki: import batch `{{BATCH}}` ({{N}} of {{TOTAL}}), unattended, on {{DATE}}. Nobody is at the terminal. Never ask a question and never wait for input. Finish the work and stop.

# Where you are

Your working directory is `{{WORK}}`.

- `src/<slug>/memory/*.md` are read-only copies of this batch's Claude Code memory files (from `~/.claude/projects/<slug>/memory/`). The `MEMORY.md` files there are the old indexes, included for orientation only.
- `wiki/` is your working copy of the wiki. `wiki/SCHEMA.md` is the contract: follow it exactly. `wiki/index.md` lists the pages that already exist; it is generated, so never edit it. Pages live in `wiki/pages/<slug>.md`.
- Read and write only inside this working directory. Never open `~/.claude/projects` or the live wiki, and never run `git`. Do not spawn subagents. Do not use the network.

# Schema

{{SCHEMA}}

# This batch

Projects (cwd → hub page slug; the hub may already exist):
{{PROJECTS}}

Source files ({{COUNT}} files, {{KB}} KB). Read every one of them:
{{FILES}}

{{GATED}}

# What you may change

{{MODE}}

# Method

1. Read `wiki/index.md`, then every existing page you might touch, so that you extend pages instead of duplicating them.
2. Read all of the source files above.
3. Compile topic pages. Fold related memory files into one page per subject; 3 to 30 source files per page is normal. A fact too small for its own page goes on the closest topic page or on the hub. Each project gets exactly one hub page under the slug given above, and that hub links to every page of the project.
4. Give every source file a home: add its path to the `sources` of the page that now holds its facts, in exactly the form listed above (`<slug>/memory/<file>.md`). A file can feed several pages. If a file holds nothing durable (obsolete, superseded, pure noise), still add it to the hub's `sources` and give it one line in a `## Not carried over` section at the end of the hub, saying why.
5. Keep what makes memory useful: concrete hosts, paths, ports, commands, versions, accounts, dates, decisions and their reasons, and every user preference and correction (as `feedback` rules with **Why:** and **How to apply:**). Drop chatter and anything the Never-record list forbids. Never copy secret values, even if a source file contains one; point at where the secret lives instead.
6. Check your work with `{{CHECK}}`. It reports source files that no page lists, frontmatter errors, dangling `[[links]]` and oversize pages. Fix everything it reports (create the missing page, or fix or unlink the link), then run it again until it reports `ok`.

When the check passes, stop. Do not write a summary file. The CLI integrates your pages, regenerates the index and commits.
