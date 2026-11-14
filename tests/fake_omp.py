#!/usr/bin/env python3
"""Stand-in for `omp -p` in tests: writes deterministic pages into the scratch dir it is given."""

import json
import os
import re
import sys
from pathlib import Path

argv = sys.argv[1:]
cwd = Path(argv[argv.index("--cwd") + 1])
prompt = argv[-1]
pages, inbox = cwd / "wiki/pages", cwd / "wiki/inbox"
if os.environ.get("FAKE_OMP_CALLS"):
    with open(os.environ["FAKE_OMP_CALLS"], "a") as fh:
        fh.write(prompt.split("\n", 1)[0][:80] + "\n")
if os.environ.get("FAKE_OMP_PROMPT"):
    with open(os.environ["FAKE_OMP_PROMPT"], "a") as fh:
        fh.write(prompt + "\n")
if os.environ.get("FAKE_OMP_DUMP") and (cwd / "src").exists():  # what the model gets to read
    with open(os.environ["FAKE_OMP_DUMP"], "a") as fh:
        fh.write("".join(f.read_text() for f in sorted((cwd / "src").rglob("*.md"))))
if os.environ.get("FAKE_OMP_LIMIT"):  # the provider refuses, as omp prints it: a 429 with a retry-after
    print("Working...")
    print('429 {"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your '
          'account\'s rate limit."},"request_id":"req_x"} retry-after-ms=120000')
    sys.exit(1)
if os.environ.get("FAKE_OMP_NOOP"):  # a run that exits 0 without writing anything
    print("fake omp did nothing")
    sys.exit(0)
once = os.environ.get("FAKE_OMP_NOOP_ONCE")  # a marker path: the first run exits 0 doing nothing, later ones work
if once and not os.path.exists(once):
    Path(once).write_text("noop done\n")
    print("fake omp did nothing (once)")
    sys.exit(0)
if os.environ.get("FAKE_OMP_EDIT") and prompt.startswith("You are compiling"):
    # the user edits a memory file while the run works on its staged copy (needs WIKI_SANDBOX=0)
    with open(os.environ["FAKE_OMP_EDIT"], "a") as fh:
        fh.write("EDITED MID-RUN\n")


def page(slug, title, kind, scope, sources, body):
    scope_txt = "scope: global" if scope == "global" else "scope:\n" + "".join(f"  - {s}\n" for s in scope).rstrip()
    src = "".join(f"  - {s}\n" for s in sources)
    (pages / f"{slug}.md").write_text(f"---\ntitle: {title}\nsummary: {title} summary\nkind: {kind}\n{scope_txt}\n"
                                      f"tags: [test]\nsources:\n{src}updated: 2026-11-13\n---\n\n{body}\n")


if prompt.startswith("You are running a serial merge pass"):
    for n in re.findall(r"^- `wiki/inbox/([^`]+)`", prompt, re.M):
        note = inbox / n
        if n.startswith("_agent--"):  # an agent note: its fact lands on a page that cites the note
            head, fact = note.read_text().split("---\n", 2)[1:]
            meta = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
            assert "gated" not in meta, f"a gated note reached the model: {n}"
            local = meta["type"] in ("project", "reference") and meta.get("cwd")
            slug = f"agent-notes-{Path(meta['cwd']).name}" if local else "agent-notes"
            target = pages / f"{slug}.md"
            if target.exists():
                target.write_text(target.read_text().replace("updated:", f"  - note/{n}\nupdated:", 1).rstrip()
                                  + f"\n- {fact.strip()}\n")
            else:
                page(slug, slug, "topic", [meta["cwd"].strip('"')] if local else "global", [f"note/{n}"],
                     f"- {fact.strip()}")
            note.unlink()
            continue
        target = pages / (n.split("--")[0] + ".md")
        if target.exists():
            target.write_text(target.read_text().rstrip() + f"\n- merged {n}\n")
        note.unlink()
elif prompt.startswith("You are compiling"):
    parallel = "Other batches run at the same time" in prompt
    projects = re.findall(r"^- `([^`]+)` → `([^`]+)` \(memory `([^`]+)`\)", prompt, re.M)
    files = re.findall(r"^- `src/([^`]+)`", prompt, re.M)
    for cwd_, hub, slug in projects:
        mine = [f for f in files if f.startswith(slug + "/")]
        topics = []
        for f in mine:
            t = re.sub(r"[^a-z0-9]+", "-", Path(f).stem.lower()).strip("-")
            topics.append(t)
            page(t, f"Topic {t}", "topic", [cwd_], [f], f"Fact from {f}. Hub: [[{hub}]].\n\n"
                 + (cwd / "src" / f).read_text().split("---", 2)[-1].strip())
        existing = (pages / f"{hub}.md")
        old = existing.read_text() if existing.exists() else ""
        prior = re.findall(r"^  - (\S+/memory/\S+)$", old, re.M)
        page(hub, f"Project {hub}", "project", [cwd_], list(dict.fromkeys(prior + mine)),
             "Hub. " + " ".join(f"[[{t}]]" for t in sorted(set(topics + re.findall(r"\[\[([^\]]+)\]\]", old)))))
    # every batch also claims a shared tool page: parallel runs collide on it
    page("shared-tool", "Shared tool", "tool", "global", [], f"Seen by {prompt.split('`')[1]}.")
    if parallel:
        inbox.mkdir(exist_ok=True)
        (inbox / "user-profile.md").write_text(f"- a user fact\nsources:\n  - {files[0]}\n")
    else:
        page("user-profile", "User profile", "user", "global", [], "The user. [[shared-tool]]")
        txt = (pages / "user-profile.md").read_text().replace("tags: [test]", "tags: [core]")
        (pages / "user-profile.md").write_text(txt)
elif "wiki \"dream\"" in prompt:
    report = re.search(r"report at `([^`]+)`", prompt).group(1)
    report_json = re.search(r"JSON twin at `([^`]+)`", prompt).group(1)
    Path(report).write_text("# Dream\n\nNothing durable.\n")
    Path(report_json).write_text(json.dumps({"summary": {"sessionsRead": 0}}))
    listed = re.findall(r"^### (\S+)$", prompt, re.M)  # the projects in the manifest
    if os.environ.get("FAKE_OMP_BOOTSTRAP"):
        for cwd_ in listed:
            sessions = re.findall(r"`([^`]+\.jsonl)`", prompt)
            page("hub-" + Path(cwd_).name, Path(cwd_).name, "project", [cwd_], sessions, f"Hub from {len(sessions)} sessions.")
    else:
        page("dream-note", "Dream note", "topic", "global", [], "From the dream. [[user-profile]]")
print("fake omp done")
