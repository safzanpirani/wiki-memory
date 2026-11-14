#!/usr/bin/env python3
"""wiki-memory: a wiki of topic pages as the long-term memory of coding agents.

Read (no model):
  wiki context [--cwd PATH] [--budget-chars N] [--instructions]   the block a session loads at start
  wiki search <terms>... [--cwd PATH] [--gated]   ranked literal search (gated pages only with --gated)
  wiki read <slug>...  |  wiki index [--cwd PATH]  |  wiki backlinks <slug>  |  wiki log [-n N]
  wiki status  |  wiki doctor  |  wiki lint [--fix]  |  wiki verify [--project CWD]

Write (no model):
  wiki note --type user|feedback|project|reference --title T [--body TEXT] [--cwd PATH]
            [--page SLUG | --gated TRIGGER]    save one fact: to inbox/ for the next fold, or with
                                               --page straight onto a page, verbatim
  wiki notes [--cwd PATH | --all]              notes not folded yet
  wiki sync                                    commit notes, pull --rebase, push (a wiki shared by machines)
  wiki history <slug>  |  wiki diff [ref]  |  wiki undo <ref> [--dry-run]

Model passes (a headless agent run in a sandbox; one at a time):
  wiki import --all | --project CWD [--dry-run] [--parallel N]   compile Claude Code memory files into pages
  wiki import --refresh-all | --refresh-project CWD | --refresh PATH...   re-import files that changed
  wiki import --gaps | --report | --mark-ingested
  wiki fold [--dry-run]                        fold inbox notes into pages
  wiki dream [--since-hours N] [--dry-run]     read recent sessions and update pages
  wiki nightly                                 sync, refresh, fold, sync: what the scheduled job runs
  wiki transcript <session.jsonl> [--from-event N]   a session's dialogue, bounded (dream reads with it)

Setup:
  wiki init                                    create the data repo
  wiki install bin|claude-code|omp|schedule [--remove] [--at HH:MM]

Settings are WIKI_* environment variables, or the same lines in ~/.config/wiki-memory/env:
WIKI_HOME, WIKI_STATE, WIKI_PROJECTS, WIKI_RUNNER (claude, codex, omp), WIKI_RUNNER_CMD, WIKI_MODEL,
WIKI_SANDBOX=0, WIKI_NOTES_ONLY=1, WIKI_EXCLUDE, WIKI_GLOBAL_DIRS, WIKI_UMBRELLA, WIKI_BATCH_CHARS,
WIKI_MAX_TIME. See README.md.

Every verb takes --json. Exit codes: 0 ok, 1 failed, 2 usage, 3 lint issues or undo conflicts left,
4 another run holds the lock, 5 stopped on a provider quota or rate limit (running batches finish, no
new one starts).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Optional


def _path(var: str, default: Path) -> Path:
    return Path(os.path.expanduser(os.environ.get(var) or str(default)))


HOME = Path.home()
CODE = Path(__file__).resolve().parent
CONFIG = _path("WIKI_CONFIG", HOME / ".config/wiki-memory")


def _load_env_file() -> None:
    """`~/.config/wiki-memory/env`: WIKI_* settings, one KEY=VALUE per line. The real environment wins."""
    try:
        lines = (CONFIG / "env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.startswith("WIKI_") and not key.startswith("#"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]  # one matching pair of outer quotes
            os.environ.setdefault(key.strip(), value)


_load_env_file()


def _dirs(var: str) -> list[str]:
    return [os.path.expanduser(d).rstrip("/") for d in os.environ.get(var, "").split(os.pathsep) if d.strip()]


WIKI = _path("WIKI_HOME", HOME / ".local/share/wiki-memory/wiki")
STATE = _path("WIKI_STATE", HOME / ".local/share/wiki-memory/state")
PROJECTS = _path("WIKI_PROJECTS", HOME / ".claude/projects")
OMP_SESSIONS = _path("WIKI_OMP_SESSIONS", HOME / ".omp/agent/sessions")
EXCLUDE_FILE = _path("WIKI_EXCLUDE", CONFIG / "exclude.txt")
# The headless agent that does the model passes: claude (`claude -p`), codex (`codex exec`), omp (`omp -p`),
# or WIKI_RUNNER_CMD, a command line with {prompt}, {cwd} and {model} placeholders.
RUNNER_BINS = ("claude", "codex", "omp")
RUNNER_CMD = shlex.split(os.environ.get("WIKI_RUNNER_CMD", ""))
RUNNER = os.environ.get("WIKI_RUNNER") or ("custom" if RUNNER_CMD else
                                           next((r for r in RUNNER_BINS if shutil.which(r)), ""))
MODEL = os.environ.get("WIKI_MODEL", "")  # empty: the runner's own default
THINKING = os.environ.get("WIKI_THINKING", "high")  # omp only
MAX_TIME = os.environ.get("WIKI_MAX_TIME", "50m")
DREAM_MAX_TIME = os.environ.get("WIKI_DREAM_MAX_TIME", "60m")
BATCH_CHARS = int(os.environ.get("WIKI_BATCH_CHARS", "220000"))
PAGES, INBOX = WIKI / "pages", WIKI / "inbox"
SELF = f"python3 {CODE / 'wiki.py'}"
RUN_LABEL = RUNNER + (f"/{MODEL}" if MODEL else "")

KINDS = ("user", "feedback", "machine", "project", "tool", "topic")
HEADINGS = {"user": "User", "feedback": "Feedback", "machine": "Machines", "project": "Projects",
            "tool": "Tools", "topic": "Topics"}
ORDER = ("title", "summary", "kind", "scope", "tags", "sources", "updated", "gated")
REQUIRED = ("title", "summary", "kind", "scope", "updated")
OVERSIZE = 8000
SUMMARY_MAX = 200
# Umbrella dirs: a page scoped to one applies only there, not to every project below it.
# WIKI_UMBRELLA adds more (a parent folder that holds many repos), separated by the path separator.
UMBRELLA = {str(HOME), *_dirs("WIKI_UMBRELLA")}
# Memory dirs whose facts are mostly cross-project: imported first, serially, with full edit rights.
# WIKI_GLOBAL_DIRS adds more (a dotfiles or ops repo whose memory is about everything).
GLOBAL_TIER = tuple(dict.fromkeys([*_dirs("WIKI_GLOBAL_DIRS"), str(HOME), str(HOME / ".claude"), str(HOME / ".omp"),
                                   *sorted(UMBRELLA)]))
SKIP_CWD = ("/tmp", "/private/tmp", "/private/var/folders", "/var/folders")
# A PEM private key is redacted whole, BEGIN through END; with no END line, through the base64 body.
SECRET = re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+"
                    r"|xox[bap]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----(?s:.*?)-----END [A-Z ]*PRIVATE KEY-----"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s\\]*")
LINK = re.compile(r"\[\[([^\]|#\n]+)(?:[#|][^\]\n]*)?\]\]")
SOURCE_REF = re.compile(r"(?<![\w/])(-[\w.-]+/memory/[^\s`\"'\])]+\.md)")
GATE = re.compile(r"(?i)\bdo[\s_-]+not[\s_-]+read[\s_-]+unless\b[\s:,_-]*(.*)")
LOG_HEAD = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] (\S+) \| (.*)$")
SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class Usage(Exception):
    """Bad argument or ref: exit 2."""


def today() -> str:
    return date.today().isoformat()


def out(args, data, text: str) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else text)


def sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


# ---- frontmatter -------------------------------------------------------------------------------

FM = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def parse_frontmatter(text: str) -> tuple[dict, str, list[str]]:
    """A small YAML subset: `key: scalar`, `key: [a, b]`, and `key:` followed by `- item` lines."""
    m = FM.match(text)
    if not m:
        return {}, text, ["no frontmatter block"]
    meta, errors, key = {}, [], None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*?)\s*$", line)
        if item and key is not None and isinstance(meta.get(key), list):
            meta[key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        if not kv:
            errors.append(f"unparsed frontmatter line: {line.strip()[:60]}")
            continue
        key, v = kv.group(1), (kv.group(2) or "").strip()
        if v.startswith("#"):
            v = ""
        if v == "":
            meta[key] = []
        elif v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[key] = [_scalar(x) for x in next(csv.reader([inner], skipinitialspace=True))] if inner else []
        else:
            meta[key] = _scalar(v)
    return meta, text[m.end():], errors


def _quote(v) -> str:
    v = str(v)
    plain = (v and v == v.strip() and v[0] not in "[]{}\"'#&*!|>%@`,?:-" and ": " not in v
             and " #" not in v and v.lower() not in ("true", "false", "null", "yes", "no", "~"))
    return v if plain else json.dumps(v, ensure_ascii=False)


def dump_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k in [*ORDER, *sorted(k for k in meta if k not in ORDER)]:
        if k not in meta:
            continue
        v = meta[k]
        if k == "scope" and v in ("global", ["global"]):
            lines.append("scope: global")
        elif k == "tags" and isinstance(v, list):
            lines.append("tags: [" + ", ".join(_quote(x) for x in v) + "]")
        elif isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {_quote(x)}" for x in v]
        else:
            lines.append(f"{k}: {_quote(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def as_list(v) -> list:
    if v is None or v == "":
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def norm_source(s: str) -> str:
    s = s.strip().strip("`")
    for root in (str(PROJECTS) + "/", "~/.claude/projects/", str(HOME / ".claude/projects") + "/"):
        if s.startswith(root):
            return s[len(root):]
    return s[2:] if s.startswith("./") else s


# ---- pages -------------------------------------------------------------------------------------

class Page:
    def __init__(self, path: Path, text: Optional[str] = None):
        self.path, self.slug = path, path.stem
        self.text = path.read_text(errors="replace") if text is None else text
        self.meta, self.body, self.errors = parse_frontmatter(self.text)

    title = property(lambda s: str(s.meta.get("title") or s.slug))
    summary = property(lambda s: " ".join(str(s.meta.get("summary") or "").split()))
    kind = property(lambda s: str(s.meta.get("kind") or "").lower())
    tags = property(lambda s: [t.lower() for t in as_list(s.meta.get("tags"))])
    sources = property(lambda s: [norm_source(x) for x in as_list(s.meta.get("sources"))])
    gated = property(lambda s: str(s.meta.get("gated") or "").strip() if not isinstance(s.meta.get("gated"), list)
                     else " ".join(s.meta.get("gated")).strip())

    @property
    def scope(self):
        v = self.meta.get("scope")
        if v in (None, "", []):
            return None
        if v in ("global", ["global"]):
            return "global"
        return [os.path.expanduser(x).rstrip("/") or "/" for x in as_list(v)]

    def links(self) -> list[str]:
        return [link_target(t) for t in LINK.findall(self.body)]

    def index_line(self) -> str:
        if self.gated:
            return f"- [[{self.slug}]] — DO NOT READ UNLESS {self.gated}"
        return f"- [[{self.slug}]] — {self.summary or '(no summary)'}"


def link_target(t: str) -> str:
    t = t.strip()
    return t[:-3] if t.endswith(".md") else t


def load_pages(root: Optional[Path] = None) -> dict[str, Page]:
    root = root or PAGES
    return {p.stem: Page(p) for p in sorted(root.glob("*.md"))} if root.exists() else {}


def normalized_text(page: Page) -> str:
    """Canonical frontmatter (missing title/scope/tags/sources/updated filled) + the untouched body."""
    meta = dict(page.meta)
    meta.setdefault("title", page.slug.replace("-", " ").title())
    if isinstance(meta.get("kind"), str):
        meta["kind"] = meta["kind"].strip().lower()
    scope = page.scope
    meta["scope"] = "global" if scope in (None, "global") else list(dict.fromkeys(scope))
    meta["tags"] = list(dict.fromkeys(t.strip().lower() for t in as_list(meta.get("tags")) if t.strip()))
    meta["sources"] = list(dict.fromkeys(norm_source(s) for s in as_list(meta.get("sources")) if s.strip()))
    if isinstance(meta.get("summary"), str):
        meta["summary"] = " ".join(meta["summary"].split())
    upd = str(meta.get("updated") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", upd):
        meta["updated"] = today()
    body = page.body.lstrip("\n")
    return dump_frontmatter(meta) + "\n" + body.rstrip() + "\n"


def page_errors(p: Page) -> list[str]:
    errs = list(p.errors)
    if not SLUG_OK.match(p.slug):
        errs.append("slug is not lowercase kebab-case")
    if p.errors == ["no frontmatter block"]:
        return errs
    for k in REQUIRED:
        if p.meta.get(k) in (None, "", []):
            errs.append(f"missing {k}")
    if p.kind and p.kind not in KINDS:
        errs.append(f"invalid kind {p.kind!r}")
    upd = p.meta.get("updated")
    if upd and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(upd)):
        errs.append(f"invalid updated {upd!r}")
    sc = p.scope
    if isinstance(sc, list) and any(not s.startswith("/") for s in sc):
        errs.append("scope entries must be absolute paths or `global`")
    if len(p.summary) > SUMMARY_MAX:
        errs.append(f"summary longer than {SUMMARY_MAX} chars")
    return errs


# ---- index, scope, context ---------------------------------------------------------------------

def render_index(pages: dict[str, Page]) -> str:
    lines = ["# Wiki index", "", "Generated by the `wiki` CLI from page frontmatter; never edit by hand.", ""]
    ps = sorted(pages.values(), key=lambda p: p.slug)
    groups = [(HEADINGS[k], [p for p in ps if not p.gated and p.kind == k]) for k in KINDS]
    groups.append(("Other", [p for p in ps if not p.gated and p.kind not in KINDS]))
    groups.append(("Gated", [p for p in ps if p.gated]))
    for head, members in groups:
        if members:
            lines += [f"## {head}", "", *(p.index_line() for p in members), ""]
    return "\n".join(lines)


def write_index() -> bool:
    text = render_index(load_pages())
    f = WIKI / "index.md"
    if f.exists() and f.read_text() == text:
        return False
    f.write_text(text)
    return True


def path_related(scope_path: str, cwd: str) -> bool:
    if scope_path == cwd:
        return True
    if cwd.startswith(scope_path.rstrip("/") + "/") and scope_path not in UMBRELLA:
        return True  # working inside the project
    return scope_path.startswith(cwd.rstrip("/") + "/") and cwd not in UMBRELLA  # a sub-project


def in_scope(p: Page, cwd: Optional[str]) -> bool:
    if cwd is None or p.scope in (None, "global"):
        return True
    return any(path_related(s, cwd) for s in p.scope)


def hubs_for(pages: dict[str, Page], cwd: str) -> list[Page]:
    """Project hubs for cwd, best first: exact scope match, then the nearest enclosing project, then
    sub-projects (shallowest first). A sub-project never outranks a project that encloses cwd."""
    def rank(s: str) -> tuple[int, int]:
        if s == cwd:
            return (0, 0)
        if cwd.startswith(s.rstrip("/") + "/"):
            return (1, -len(s))  # enclosing: the deepest (nearest) first
        return (2, len(s))  # a sub-project of cwd

    ranked = []
    for p in pages.values():
        if p.kind != "project" or p.gated or not isinstance(p.scope, list):
            continue
        best = min((rank(s) for s in p.scope if path_related(s, cwd)), default=None)
        if best is not None:
            ranked.append((best, p.slug, p))
    return [p for *_, p in sorted(ranked)]


def resolve_cwd(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    return str(Path(os.path.expanduser(path)).resolve()).rstrip("/") or "/"


def scoped_index(pages: dict[str, Page], cwd: str) -> list[tuple[str, list[str]]]:
    ps = sorted(pages.values(), key=lambda p: p.slug)
    hub_slugs = [h.slug for h in hubs_for(pages, cwd)]
    local = [p for p in ps if not p.gated and isinstance(p.scope, list) and in_scope(p, cwd)]
    local.sort(key=lambda p: (p.slug not in hub_slugs, hub_slugs.index(p.slug) if p.slug in hub_slugs else 0,
                              KINDS.index(p.kind) if p.kind in KINDS else 9, p.slug))
    sections = [("This project", [p.index_line() for p in local])]
    sections.append(("Gated (open only when the task matches the trigger)",
                     [p.index_line() for p in ps if p.gated and in_scope(p, cwd)]))
    for kind in ("feedback", "user", "machine", "tool", "topic", "project"):
        sections.append((f"Global {HEADINGS[kind].lower()}",
                         [p.index_line() for p in ps if not p.gated and p.scope in (None, "global") and p.kind == kind]))
    return [(h, lines) for h, lines in sections if lines]


def page_block(p: Page) -> str:
    return f"### [[{p.slug}]] — {p.title}\n\n{p.body.strip()}\n"


def build_context(cwd: str, budget: int) -> dict:
    pages = load_pages()
    hubs = hubs_for(pages, cwd)
    head = (f"# Wiki memory for {cwd}\n\n"
            f"Agent wiki at `{WIKI}` (pages/<slug>.md, one topic per page). This block holds the scoped "
            "index plus the project hub and core pages. Open more with `wiki read <slug>`; find with "
            "`wiki search <terms> --cwd .`. Gated pages: read only when the task matches the trigger.")
    sections = scoped_index(pages, cwd)
    bodies = []
    seen = set()
    core = [p for p in pages.values() if not p.gated and "core" in p.tags and in_scope(p, cwd)]
    users = [p for p in pages.values() if not p.gated and p.kind == "user" and in_scope(p, cwd)]
    # Core rules first (they apply to every session), then the hub (truncated if it must be), then user pages.
    for p in [*sorted(core, key=lambda p: (p.kind != "user", p.slug)), *hubs[:1],
              *sorted(users, key=lambda p: p.slug)]:
        if p.slug not in seen:
            seen.add(p.slug)
            bodies.append(p)
    used = len(head)
    index_cap = int(budget * 0.4)
    kept: list[tuple[str, list[str], int]] = []  # heading, kept lines, total lines

    def take_lines(cap: int) -> None:
        nonlocal used
        for i, (h, lines) in enumerate(sections):
            if i >= len(kept):
                kept.append((h, [], len(lines)))
            have = kept[i][1]
            for line in lines[len(have):]:
                cost = len(line) + 1 + (len(h) + 6 if not have else 0)
                if used + cost > cap:
                    return
                have.append(line)
                used += cost

    take_lines(index_cap)
    included, truncated = [], []
    page_texts = []
    for p in bodies:
        block = page_block(p)
        room = budget - used - 40
        if len(block) <= room:
            page_texts.append(block)
            used += len(block) + 1
            included.append(p.slug)
        elif p in hubs[:1] and room > 600:  # the hub matters most: keep its head
            cut = block[:room - 80].rsplit("\n", 1)[0]
            page_texts.append(cut + f"\n… (truncated; `wiki read {p.slug}`)\n")
            used += len(page_texts[-1]) + 1
            included.append(p.slug)
            truncated.append(p.slug)
    take_lines(budget - 120)  # leftover budget goes back to the index

    def assemble() -> str:
        parts = [head]
        if kept:
            parts.append("## Index")
        for (h, have, total) in kept:
            if not have:
                parts.append(f"### {h}\n(+{total} pages: `wiki index --cwd {cwd}`)")
                continue
            more = f"\n(+{total - len(have)} more: `wiki index --cwd {cwd}`)" if total > len(have) else ""
            parts.append(f"### {h}\n" + "\n".join(have) + more)
        if page_texts:
            parts.append("## Pages\n\n" + "\n".join(page_texts))
        return "\n\n".join(parts).rstrip() + "\n"

    text = assemble()
    while len(text) > budget:  # the estimates above ignore separators; trim index lines, then pages
        many = next((have for _, have, _ in reversed(kept) if len(have) > 1), None)
        if many:
            many.pop()
        elif page_texts:
            page_texts.pop()
            slug = included.pop()
            if slug in truncated:
                truncated.remove(slug)
        elif any(have for _, have, _ in kept):
            next(have for _, have, _ in reversed(kept) if have).pop()
        elif kept:
            kept.pop()  # an index section reduced to its "+N pages" stub
        else:
            break
        text = assemble()
    header_cut = len(text) > budget
    if header_cut:  # the budget is below the minimal header: cut the header itself, with a note
        note = f"\n… (header cut to --budget-chars {budget}; it needs {len(text)})\n"
        text = text[:budget - len(note)].rstrip() + note if budget > len(note) + 20 else text[:budget]
    return {"cwd": cwd, "budget": budget, "chars": len(text), "hubs": [h.slug for h in hubs],
            "pagesIncluded": included, "truncated": truncated, "headerCut": header_cut, "text": text}


NOTE_PREFIX = "_agent--"
NOTE_TYPES = ("user", "feedback", "project", "reference")


def model_host_check() -> None:
    """Model passes rewrite pages, so one host runs them. On every other host that shares the wiki set
    WIKI_NOTES_ONLY=1: it then only adds notes and syncs, and two hosts never edit the same page."""
    if os.environ.get("WIKI_NOTES_ONLY") == "1":
        raise Usage("WIKI_NOTES_ONLY=1: this host only adds notes (`wiki note`) and syncs (`wiki sync`); "
                    "run import, dream and fold on the host that owns the pages")


def pending_notes() -> list[dict]:
    """Agent notes waiting in inbox/ for `wiki fold`, newest first."""
    notes = []
    for f in INBOX.glob(f"{NOTE_PREFIX}*.md") if INBOX.exists() else ():
        try:
            text = f.read_text(errors="replace")
        except OSError:  # a fold removed it between the listing and the read
            continue
        meta, body, _ = parse_frontmatter(text)
        notes.append({"file": f.name, "title": str(meta.get("title") or f.stem), "type": str(meta.get("type") or ""),
                      "cwd": str(meta.get("cwd") or ""), "host": str(meta.get("host") or ""),
                      "created": str(meta.get("created") or ""),
                      "gated": " ".join(str(meta.get("gated") or "").split()) or None, "body": body.strip()})

    def when(n: dict) -> float:  # hosts write their own UTC offsets, so compare instants, not strings
        try:
            return datetime.fromisoformat(n["created"]).timestamp()
        except ValueError:
            return 0.0
    notes.sort(key=lambda n: (when(n), n["file"]), reverse=True)
    return notes


def note_gate(path: Path) -> Optional[str]:
    """The trigger of an inbox note, None for an open one. A note that cannot be read counts as gated."""
    try:
        meta, _, _ = parse_frontmatter(path.read_text(errors="replace"))
    except OSError:
        return "unreadable"
    return " ".join(str(meta.get("gated") or "").split()) or None


def fold_gated_notes() -> list[str]:
    """Gated agent notes go into gated pages verbatim, without any model seeing them: one page of
    notes per trigger (`gated-notes-<trigger>-<hash>`), apart from the verbatim copies of gated memory
    files, which a refresh rewrites whole. Returns the note files folded."""
    done = []
    for n in reversed(pending_notes()):
        if not n["gated"]:
            continue
        words = re.sub(r"[^a-z0-9]+", "-", n["gated"].lower()).strip("-").split("-")
        # the hash keeps two triggers that start alike on separate pages
        f = PAGES / ("-".join(["gated-notes", *words[:6], sha(n["gated"].lower())[:6]]) + ".md")
        src = f"note/{n['file']}"
        entry = f"\n## {n['title']} ({n['created'][:10]}, {n['host']}, {n['type']})\n\n{n['body']}\n"
        if f.exists():
            page = Page(f)
            if page.gated.lower() != n["gated"].lower():  # not this trigger's page: the note waits
                continue
            meta = dict(page.meta)
            meta["sources"] = list(dict.fromkeys([*page.sources, src]))
            meta["updated"] = today()
            if n["cwd"] and isinstance(page.scope, list) and n["cwd"] not in page.scope:
                meta["scope"] = [*page.scope, n["cwd"]]
            text = dump_frontmatter(meta) + page.body.rstrip("\n") + "\n" + entry
        else:
            meta = {"title": f"Gated notes: {n['gated'][:70]}", "summary": f"DO NOT READ UNLESS {n['gated']}"[:SUMMARY_MAX],
                    "kind": "topic", "scope": [n["cwd"]] if n["cwd"] else "global", "tags": ["gated"],
                    "sources": [src], "updated": today(), "gated": n["gated"]}
            text = (dump_frontmatter(meta) + "\nNotes agents saved with `wiki note --gated`, placed here by the "
                    "wiki CLI without any model reading them.\n" + entry)
        tmp = WIKI / ".git" / f"page-{os.getpid()}-{os.urandom(3).hex()}.tmp"
        tmp.write_text(text)
        tmp.replace(f)  # the page is whole before its note goes
        (INBOX / n["file"]).unlink(missing_ok=True)
        done.append(n["file"])
    return done


def note_in_scope(n: dict, cwd: Optional[str]) -> bool:
    if n["gated"]:
        return False
    if cwd is None or n["type"] in ("user", "feedback") or not n["cwd"]:
        return True
    return n["cwd"] == cwd or path_related(n["cwd"], cwd)


def notes_block(cwd: str, cap: int) -> tuple[str, list[str]]:
    notes = [n for n in pending_notes() if note_in_scope(n, cwd)]
    if not notes or cap < 200:
        return "", []
    head = ("## Recent notes (saved by agents, not yet folded into pages)\n\n"
            "Newer than the pages above; when they disagree, the note wins.\n")
    more = "\n(+{} more: `wiki notes`)\n"
    parts, used, shown = [head], len(head) + len(more) + 4, []  # the footer is part of the cap
    for n in notes:
        body = n["body"] if len(n["body"]) <= 900 else n["body"][:880].rsplit(" ", 1)[0] + " …"
        block = f"\n### {n['title']} ({n['type'] or 'note'}, {n['created'][:10]}, {n['host']})\n{body}\n"
        if used + len(block) > cap:
            break
        parts.append(block)
        used += len(block)
        shown.append(n["file"])
    if not shown:
        return "", []
    if len(shown) < len(notes):
        parts.append(more.format(len(notes) - len(shown)))
    return "".join(parts), shown


def context_with_notes(cwd: str, budget: int) -> dict:
    block, shown = notes_block(cwd, min(4000, budget // 4))
    data = build_context(cwd, budget - len(block) - 2 if block else budget)  # block <= budget / 4
    if block:
        data["text"] = (data["text"].rstrip() + "\n\n" + block).rstrip()
        data["chars"] = len(data["text"])
    data["notesIncluded"] = shown
    return data


# ---- git, lock, log ----------------------------------------------------------------------------

def git(*args: str, check: bool = True, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd or WIKI), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def git_bytes(*args: str) -> Optional[bytes]:
    r = subprocess.run(["git", "-C", str(WIKI), *args], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def ensure_repo() -> None:
    if not (WIKI / ".git").exists():
        raise Usage(f"no wiki at {WIKI}; run `wiki init`")


def init_repo() -> bool:
    created = not (WIKI / ".git").exists()
    PAGES.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(exist_ok=True)
    for d in (PAGES, INBOX):
        (d / ".gitkeep").touch()
    if not (WIKI / "SCHEMA.md").exists():
        shutil.copy(CODE / "schema.md", WIKI / "SCHEMA.md")
    if not (WIKI / "log.md").exists():
        (WIKI / "log.md").write_text("# Wiki log\n\nAppend-only. The `wiki` CLI adds one entry per import batch, "
                                     "merge, dream, lint fix and undo.\n")
    if created:
        git("init", "-q", "-b", "main")
    write_index()
    if created:
        append_log("init", "wiki created", [f"home {WIKI}"])
        commit("init")
    return created


class Lock:
    """One import/dream/undo/fix at a time, across processes."""

    def __init__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.fh = open(STATE / "wiki.lock", "w")

    def __enter__(self):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Locked()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


class Locked(Exception):
    pass


def append_log(op: str, title: str, lines: list[str]) -> None:
    with open(WIKI / "log.md", "a") as f:
        f.write(f"\n## [{today()}] {op} | {title}\n\n" + "".join(f"- {line}\n" for line in lines))


def redact_file(path: Path) -> bool:
    """Redact token-shaped text in one file in place (bytes it cannot decode survive). True if changed."""
    try:
        text = path.read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return False
    new = SECRET.sub("[REDACTED-SECRET]", text)
    if new == text:
        return False
    path.write_bytes(new.encode("utf-8", "surrogateescape"))
    return True


def redact_changed() -> list[str]:
    """Token-shaped text never reaches git: redact it in changed files before staging."""
    flagged = []
    st = git("status", "--porcelain", "-z", "--untracked-files=all").stdout
    for entry in filter(None, st.split("\0")):
        path = WIKI / entry[3:]
        if path.is_file() and path.suffix == ".md" and redact_file(path):
            flagged.append(entry[3:])
    return flagged


def commit(message: str) -> Optional[str]:
    write_index()
    flagged = redact_changed()
    if flagged:
        append_log("redact", message, [f"token-shaped text redacted in {f}" for f in flagged])
    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    git("commit", "-q", "-m", message)
    return git("rev-parse", "HEAD").stdout.strip()


def committed_subjects() -> set[str]:
    r = git("log", "--format=%s", check=False)
    return set(r.stdout.splitlines()) if r.returncode == 0 else set()


def last_commit(grep: str) -> Optional[dict]:
    r = git("log", "-1", "--format=%H%x09%cI%x09%s", f"--grep={grep}", check=False)
    if r.returncode or not r.stdout.strip():
        return None
    h, when, subj = r.stdout.strip().split("\t", 2)
    return {"commit": h[:7], "date": when, "subject": subj}


# ---- lint --------------------------------------------------------------------------------------

def lint(pages: dict[str, Page]) -> dict:
    inbound: dict[str, set] = {s: set() for s in pages}
    dangling = []
    for p in pages.values():
        if p.gated:
            continue  # verbatim copies of memory files: their links point at old memory slugs
        for t in p.links():
            if t in pages:
                if t != p.slug:
                    inbound[t].add(p.slug)
            else:
                dangling.append({"page": p.slug, "link": t})
    orphans = [s for s, src in inbound.items() if not src and pages[s].kind != "project" and not pages[s].gated]
    frontmatter = [{"page": p.slug, "error": e} for p in pages.values() for e in page_errors(p)]
    oversize = [{"page": p.slug, "chars": len(p.text)} for p in pages.values()
                if len(p.text) > OVERSIZE and not p.gated]
    titles: dict[str, list[str]] = {}
    for p in pages.values():
        if not p.gated:
            titles.setdefault(p.title.strip().lower(), []).append(p.slug)
    duplicates = [{"title": t, "pages": s} for t, s in titles.items() if len(s) > 1]
    idx = WIKI / "index.md"
    drift = not idx.exists() or idx.read_text() != render_index(pages)
    inbox = sorted(f.name for f in INBOX.glob("*.md")) if INBOX.exists() else []
    res = {"pages": len(pages), "dangling": dangling, "orphans": sorted(orphans), "frontmatter": frontmatter,
           "oversize": oversize, "duplicateTitles": duplicates, "indexDrift": drift, "inbox": inbox}
    res["issues"] = sum(len(res[k]) for k in ("dangling", "orphans", "frontmatter", "oversize",
                                               "duplicateTitles", "inbox")) + int(drift)
    return res


def lint_summary(res: dict) -> str:
    return (f"{res['pages']} pages: {len(res['dangling'])} dangling links, {len(res['orphans'])} orphans, "
            f"{len(res['frontmatter'])} frontmatter errors, {len(res['oversize'])} oversize, "
            f"{len(res['duplicateTitles'])} duplicate titles, index drift {'yes' if res['indexDrift'] else 'no'}, "
            f"{len(res['inbox'])} inbox notes")


def lint_text(res: dict, limit: int = 0) -> str:
    def cap(xs):
        return xs if not limit else xs[:limit]
    lines = [lint_summary(res)]
    lines += [f"  dangling  [[{d['link']}]] in {d['page']}" for d in cap(res["dangling"])]
    lines += [f"  orphan    {s}" for s in cap(res["orphans"])]
    lines += [f"  frontmatter {e['page']}: {e['error']}" for e in cap(res["frontmatter"])]
    lines += [f"  oversize  {o['page']} ({o['chars']} chars)" for o in cap(res["oversize"])]
    lines += [f"  duplicate title {d['title']!r}: {', '.join(d['pages'])}" for d in cap(res["duplicateTitles"])]
    lines += [f"  inbox     {n}" for n in cap(res["inbox"])]
    return "\n".join(lines)


def fix_pages() -> list[str]:
    changed = []
    for p in load_pages().values():
        if p.errors == ["no frontmatter block"]:
            continue
        new = normalized_text(p)
        if new != p.text:
            p.path.write_text(new)
            changed.append(p.slug)
    return changed


# ---- memory sources ----------------------------------------------------------------------------

def excluded_dirs() -> tuple[str, ...]:
    """exclude.txt (shared with dream): one directory per line, `~` allowed, `#` starts a comment."""
    if not EXCLUDE_FILE.exists():
        return ()
    lines = (line.split("#", 1)[0].strip() for line in EXCLUDE_FILE.read_text().splitlines())
    return tuple(os.path.expanduser(line).rstrip("/") for line in lines if line)


def is_excluded(cwd: str, dirs: tuple[str, ...]) -> bool:
    """cwd is one of dirs or below one, compared at path boundaries (also after resolving symlinks)."""
    cands = {cwd.rstrip("/") or "/", os.path.realpath(cwd)}
    roots = {x.rstrip("/") or "/" for d in dirs for x in (d, os.path.realpath(d))}
    return any(c == d or c.startswith(d.rstrip("/") + "/") for c in cands for d in roots)


def memory_dir_excluded(slug: str, cwd: str, how: str, dirs: tuple[str, ...]) -> bool:
    """Whether a memory dir belongs to an excluded project. `how` says where its cwd came from (see
    slug_to_cwd). A session header or an existing directory the slug decodes to is a real path, compared
    at path boundaries, so `~/Development/projects/cyberkit` or `cyber-tools` is not caught by an excluded
    `cyber`. Slugs are ambiguous (`/`, `.` and `-` all become `-`); when `cyber/tools` and `cyber-tools`
    both exist, the decode prefers the exact sibling name, which is then kept. Only when the cwd is a
    guess (no session, no such directory) does the slug decide, failing closed: equal to an excluded
    dir's slug or starting with it followed by `-`."""
    if is_excluded(cwd, dirs):
        return True
    if how != "guess":
        return False
    return any(slug == s or slug.startswith(s + "-") for s in map(path_slug, dirs))


def path_slug(p: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def session_header(path: Path) -> tuple[str, str]:
    """(cwd, title) from a session's first lines (same rules as dream.py)."""
    cwd, title, prompt = "", "", ""
    try:
        fh = path.open("rb")
    except OSError:
        return "", ""
    with fh:
        for i, line in enumerate(fh):
            if i >= 40:
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "title":
                title = obj.get("title", "")
            elif kind == "custom-title":
                title = obj.get("customTitle", "") or title
            elif kind == "summary" and not title:
                title = obj.get("summary", "")
            elif kind == "session":
                cwd = obj.get("cwd", "")
                break
            elif obj.get("cwd") and not cwd:
                cwd = obj["cwd"]
            if kind == "user" and not prompt:
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = next((c.get("text", "") for c in content
                                    if isinstance(c, dict) and c.get("type") == "text"), "")
                prompt = " ".join(str(content or "").split())[:60]
            if cwd and title:
                break
    return cwd, title or prompt


def slug_to_cwd(slug: str, d: Path) -> tuple[str, str]:
    """Claude's project slug drops `/` and `.`; recover the cwd from a session file or the filesystem.
    Returns (cwd, how): `session` (a session header names it), `fs` (an existing directory whose slug
    is exactly this one; longer directory names are tried first) or `guess` (the directory is gone)."""
    for j in sorted(d.glob("*.jsonl"))[:10]:
        cwd, _ = session_header(j)
        if cwd and path_slug(cwd) == slug:
            return cwd.rstrip("/") or "/", "session"

    def walk(base: str, rest: str) -> Optional[str]:
        if not rest:
            return base or "/"
        try:
            entries = [e for e in os.listdir(base or "/") if os.path.isdir(os.path.join(base or "/", e))]
        except OSError:
            return None
        for e in sorted(entries, key=len, reverse=True):
            s = "-" + path_slug(e)
            if rest == s or rest.startswith(s + "-"):
                hit = walk(f"{base}/{e}", rest[len(s):])
                if hit:
                    return hit
        return None

    hit = walk("", slug)
    if hit:
        return hit, "fs"
    # The directory is gone: follow existing parents greedily, keep the rest as one dashed name.
    base, rest = "", slug
    while rest:
        try:
            entries = [e for e in os.listdir(base or "/") if os.path.isdir(os.path.join(base or "/", e))]
        except OSError:
            break
        nxt = next((e for e in sorted(entries, key=len, reverse=True)
                    if rest.startswith("-" + path_slug(e) + "-")), None)
        if not nxt:
            break
        base, rest = f"{base}/{nxt}", rest[len(path_slug(nxt)) + 1:]
    if slug.startswith("-home-") and slug.count("-") >= 3 and not base.startswith("/home/"):
        user, _, tail = slug[len("-home-"):].partition("-")  # a remote host's /home/<user>/...
        return f"/home/{user}/{tail}", "guess"
    return (f"{base}/{rest.lstrip('-')}" if rest else (base or "/")), "guess"


def frontmatter_scalars(block: str) -> tuple[dict[str, str], bool]:
    """Top-level values of a memory file's frontmatter, each folded to one line: YAML block scalars
    (`key: >` / `key: |`), indented continuation lines and nested items join the key's value. The flag
    is False when some line fits none of those shapes."""
    parts: dict[str, list[str]] = {}
    ok, key = True, None
    for line in block.splitlines():
        kv = re.match(r"^([A-Za-z_][\w-]*):(?:[ \t]+(.*))?$", line)
        if kv:
            key, v = kv.group(1), (kv.group(2) or "").strip()
            parts[key] = [] if v == "" or re.fullmatch(r"[>|][+-]?\d*[+-]?", v) else [v]
        elif not line.strip() or line.startswith("#"):
            continue
        elif key is not None and (line[:1] in " \t" or line.startswith("- ")):
            parts[key].append(line.strip())
        else:
            ok = False
    return {k: str(_scalar(" ".join(v))) for k, v in parts.items()}, ok


def gate_trigger(f: Path) -> Optional[str]:
    """Trigger text when the file name or a frontmatter value says DO NOT READ UNLESS (spaces, `-` or
    `_` between the words). Fails closed: when the frontmatter is missing or does not parse, the phrase
    anywhere in the first 20 lines gates the file."""
    def trigger(m: re.Match, text: str) -> str:
        return m.group(1).strip().rstrip(".").strip('"').strip() or text

    stem = f.stem if " " in f.stem else re.sub(r"[-_]+", " ", f.stem)
    text = f.read_text(errors="replace")
    fm = FM.match(text)
    meta, ok = frontmatter_scalars(fm.group(1)) if fm else ({}, False)
    for value in (stem, meta.get("name", ""), meta.get("description", ""), *meta.values()):
        m = GATE.search(value)
        if m:
            return trigger(m, value)
    if not ok:  # one block, newlines kept, so a phrase wrapped across lines still matches
        head = "\n".join(text.splitlines()[:20])
        m = GATE.search(head)
        if m:
            return trigger(m, " ".join(m.group(0).split()))
    return None


def hub_slugs(cwds: list[str]) -> dict[str, str]:
    def base(c: str, depth: int) -> str:
        if c == str(HOME):
            return "home"
        parts = [p for p in c.split("/") if p][-depth:]
        return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-") or "root"

    out: dict[str, str] = {}
    pending = sorted(set(cwds))
    depth = 1
    while pending and depth < 8:
        names: dict[str, list[str]] = {}
        for c in pending:
            names.setdefault(base(c, depth), []).append(c)
        pending = []
        for name, cs in names.items():
            if len(cs) == 1 and name not in out.values():
                out[cs[0]] = name
            else:
                pending += cs
        depth += 1
    for c in pending:
        out[c] = path_slug(c).lower().strip("-")
    return out


def source_projects() -> list[dict]:
    """Non-excluded memory dirs: slug, cwd, hub slug, files (rel paths), gated files, sizes."""
    dirs = excluded_dirs()
    found = []
    for d in sorted(PROJECTS.glob("*/memory")):
        slug = d.parent.name
        cwd, how = slug_to_cwd(slug, d.parent)
        if memory_dir_excluded(slug, cwd, how, dirs):
            continue
        files, gated, chars = [], [], 0
        for f in sorted(d.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            rel = f"{slug}/memory/{f.name}"
            trig = gate_trigger(f)
            if trig:
                gated.append({"rel": rel, "trigger": trig})
                continue
            size = f.stat().st_size
            files.append({"rel": rel, "chars": size})
            chars += size
        if files or gated:
            found.append({"slug": slug, "cwd": cwd, "files": files, "gated": gated, "chars": chars})
    hubs = hub_slugs([p["cwd"] for p in found])
    for p in found:
        p["hub"] = hubs[p["cwd"]]
    return found


def copy_gated(projects: list[dict]) -> list[str]:
    """Gated memory files go into gated pages verbatim, without any model seeing them."""
    written = []
    used = {p.slug: p for p in load_pages().values()}
    for proj in projects:
        for g in proj["gated"]:
            src = PROJECTS / g["rel"]
            words = re.sub(r"[^a-z0-9]+", "-", g["trigger"].lower()).strip("-").split("-")
            base = "-".join(["gated", *words[:8]]).strip("-")
            owner = next((s for s, p in used.items() if g["rel"] in p.sources and p.gated), None)
            # Two gated files with the same trigger must not share a page: the base slug when free,
            # else one suffixed with a hash of the source path (stable across runs).
            digest = sha(g["rel"])
            slug = owner or next(s for s in (base, *(f"{base}-{digest[:n]}" for n in (6, 10, 40)))
                                 if s not in used and not (PAGES / f"{s}.md").exists())
            meta = {"title": f"Gated: {g['trigger'][:80]}", "summary": f"DO NOT READ UNLESS {g['trigger']}"[:SUMMARY_MAX],
                    "kind": "topic", "scope": [proj["cwd"]], "tags": ["gated"], "sources": [g["rel"]],
                    "updated": date.fromtimestamp(src.stat().st_mtime).isoformat(), "gated": g["trigger"]}
            text = (dump_frontmatter(meta) + "\nVerbatim copy of the gated memory file `" + g["rel"] +
                    "`, made by the wiki CLI without any model reading it.\n\n" + src.read_text(errors="replace"))
            f = PAGES / f"{slug}.md"
            if not f.exists() or f.read_text() != text:
                f.write_text(text)
                written.append(slug)
            used[slug] = Page(f, text)
    return written


def coverage(projects: Optional[list[dict]] = None) -> dict:
    projects = source_projects() if projects is None else projects
    pages = load_pages()
    referenced = {s for p in pages.values() for s in p.sources}
    allf = [f["rel"] for p in projects for f in p["files"]]
    gated = [g["rel"] for p in projects for g in p["gated"]]
    missing = [r for r in allf if r not in referenced]
    covered = len(allf) - len(missing)
    return {"files": len(allf), "covered": covered, "missing": missing,
            "pct": round(100 * covered / len(allf), 1) if allf else 100.0,
            "gated": len(gated), "gatedCovered": sum(1 for g in gated if g in referenced)}


# ---- import planning ---------------------------------------------------------------------------

def chunk(files: list[dict], cap: int) -> list[list[dict]]:
    total = sum(f["chars"] for f in files)
    n = max(1, -(-total // cap))
    target = total / n
    chunks, cur, size = [], [], 0
    for f in files:
        if cur and size + f["chars"] > target * 1.08 and len(chunks) < n - 1:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(f)
        size += f["chars"]
    return chunks + ([cur] if cur else [])


def make_batch(name: str, tier: str, parts: list[tuple[dict, list[dict]]], chain: str) -> dict:
    return {"name": name, "tier": tier, "chain": chain,
            "projects": [{"slug": p["slug"], "cwd": p["cwd"], "hub": p["hub"]} for p, _ in parts],
            "files": [f["rel"] for _, fs in parts for f in fs],
            "chars": sum(f["chars"] for _, fs in parts for f in fs)}


def make_plan(projects: list[dict], cap: int = BATCH_CHARS) -> list[dict]:
    """Global tier first (serial), then project chains (one per big project, chunks in order) and packs
    of small projects. Packs are filled in slug order so related projects share a run."""
    with_files = [p for p in projects if p["files"]]
    by_cwd = {p["cwd"]: p for p in with_files}
    plan: list[dict] = []
    g = 0
    small_global = []
    for cwd in GLOBAL_TIER:
        p = by_cwd.get(cwd)
        if not p:
            continue
        if p["chars"] < cap * 0.4:
            small_global.append(p)
            continue
        for i, fs in enumerate(chunk(p["files"], cap), 1):
            g += 1
            plan.append(make_batch(f"g{g:02d}-{p['hub']}-{i}", "global", [(p, fs)], "global"))
    if small_global:
        g += 1
        plan.append(make_batch(f"g{g:02d}-global-pack", "global", [(p, p["files"]) for p in small_global], "global"))
    rest = [p for p in with_files if p["cwd"] not in GLOBAL_TIER]
    big = sorted((p for p in rest if p["chars"] >= cap * 0.5), key=lambda p: -p["chars"])
    for p in big:
        for i, fs in enumerate(chunk(p["files"], cap), 1):
            plan.append(make_batch(f"p-{p['hub']}-{i}", "project", [(p, fs)], p["hub"]))
    packs, cur, size = [], [], 0
    for p in sorted((p for p in rest if p["chars"] < cap * 0.5), key=lambda p: p["slug"]):
        if cur and size + p["chars"] > cap:
            packs.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += p["chars"]
    if cur:
        packs.append(cur)
    for i, members in enumerate(packs, 1):
        name = f"pack-{i:02d}"
        plan.append(make_batch(name, "project", [(p, p["files"]) for p in members], name))
    return plan


def find_project(projects: list[dict], cwd: str) -> dict:
    p = next((p for p in projects if p["slug"] == path_slug(cwd) or p["cwd"] == cwd), None)
    if p is None:
        raise Usage(f"no importable memory for {cwd} (slug {path_slug(cwd)}): none, or excluded")
    return p


def project_plan(projects: list[dict], cwd: str) -> list[dict]:
    """`--project`: that project's files only, under their own batch names (`proj-<hub>-N`), never the
    stored plan's packs, which mix in other projects."""
    p = find_project(projects, cwd)
    return [make_batch(f"proj-{p['hub']}-{i}", "global", [(p, fs)], f"proj-{p['hub']}")
            for i, fs in enumerate(chunk(p["files"], BATCH_CHARS), 1) if fs]


def source_rel(path: str) -> str:
    """A memory file argument as `<slug>/memory/<file>.md`: relative to the projects root, or absolute."""
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        return norm_source(path)
    root, real = os.path.realpath(PROJECTS), os.path.realpath(p)
    if not real.startswith(root + "/"):
        raise Usage(f"{path} is not under {PROJECTS}")
    return real[len(root) + 1:]


def ingested() -> dict[str, dict]:
    """The ingestion ledger, `state/ingested.json`: source rel path -> {sha256 of its content as staged,
    staged_at}."""
    f = STATE / "ingested.json"
    return json.loads(f.read_text()) if f.exists() else {}


def note_ingested(hashes: dict[str, str], when: float) -> None:
    """Record that the wiki took in these contents (rel -> sha256). Atomic (temp file + rename); callers
    hold the import lock, and INTEGRATE inside an import."""
    STATE.mkdir(parents=True, exist_ok=True)
    data = {**ingested(), **{r: {"sha256": h, "staged_at": when} for r, h in hashes.items()}}
    tmp = STATE / f"ingested.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, STATE / "ingested.json")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stale_sources(p: dict, record: bool = False) -> list[str]:
    """--refresh-project: the project's files the wiki does not hold as they are now. A file is stale when
    no page lists it, or when its content differs from what the ledger says was ingested. A file the
    ledger lacks (imported before the ledger existed) falls back to mtime against the last non-refresh
    commit that changed how often its path appears in pages (a refresh that failed may have committed
    partway, so its commits never count); with `record` (a real run, under the import lock) the
    ones that check finds current go into the ledger, so the fallback decides each file once."""
    ledger, referenced = ingested(), referenced_sources()
    out, fresh = [], {}
    for f in p["files"]:
        rel = f["rel"]
        if rel not in referenced:
            out.append(rel)
            continue
        digest = file_sha256(PROJECTS / rel)  # before the mtime check: an edit after it reads as stale
        if rel in ledger:
            if digest != ledger[rel]["sha256"]:
                out.append(rel)
            continue
        r = git("log", "-1", "--format=%ct", "--invert-grep", "--grep=^import refresh-", f"-S{rel}",
                "--", "pages/", check=False) if (WIKI / ".git").exists() else None
        seen = int(r.stdout.strip() or 0) if r and r.returncode == 0 else 0
        if (PROJECTS / rel).stat().st_mtime > seen:
            out.append(rel)
        else:
            fresh[rel] = digest
    if record and fresh:
        note_ingested(fresh, time.time())
    return out


def refresh_plan(projects: list[dict], rels: list[str]) -> list[dict]:
    """`--refresh`/`--refresh-project`: the given non-gated files, per project, as batches
    `refresh-<YYYYMMDD-HHMMSS>-<rand>-N` (unique per run, so a later run never finds them done) whose
    prompt says the files supersede the pages."""
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    want = set(rels)
    parts = [(p, [f for f in p["files"] if f["rel"] in want]) for p in projects]
    batches, n = [], 0
    for p, fs in parts:
        for piece in chunk(fs, BATCH_CHARS) if fs else []:
            n += 1
            name = f"refresh-{stamp}-{n}"
            batches.append({**make_batch(name, "refresh", [(p, piece)], name), "refresh": True})
    return batches


def refresh_targets(args, projects: list[dict], save: bool = False) -> tuple[list[str], list[str]]:
    """(files to refresh with a model, gated files among them). Paths that are not importable memory
    files (missing, MEMORY.md, excluded) are a usage error. `--refresh-all` takes every project's
    stale files: `projects` already leaves out excluded projects and gated files."""
    if args.refresh_all:
        return [r for p in projects for r in stale_sources(p, record=save)], []
    if args.refresh_project:
        return stale_sources(find_project(projects, resolve_cwd(args.refresh_project)), record=save), []
    files = {f["rel"] for p in projects for f in p["files"]}
    gated = {g["rel"] for p in projects for g in p["gated"]}
    rels = list(dict.fromkeys(source_rel(x) for x in args.refresh))
    bad = [r for r in rels if r not in files and r not in gated]
    if bad:
        raise Usage("not importable memory files (missing, MEMORY.md, or excluded): " + ", ".join(bad))
    return [r for r in rels if r in files], [r for r in rels if r in gated]


def revalidate(batch: dict, projects: list[dict]) -> tuple[dict, list[str]]:
    """A stored plan can predate exclude.txt or gating changes. Keep only files that are still non-gated
    memory of a non-excluded project (gated ones go to copy_gated instead) and recount the batch."""
    live = {f["rel"]: f["chars"] for p in projects for f in p["files"]}
    slugs = {p["slug"] for p in projects}
    files = [r for r in batch["files"] if r in live]
    dropped = [r for r in batch["files"] if r not in live]
    return ({**batch, "files": files, "chars": sum(live[r] for r in files),
             "projects": [p for p in batch["projects"] if p["slug"] in slugs]}, dropped)


def referenced_sources() -> set[str]:
    return {s for p in load_pages().values() for s in p.sources}


def batch_committed(name: str, subjects: set[str]) -> bool:
    """A commit `import <name>` or `import <name>-retry` exists, clean or `(partial, …)`."""
    names = (f"import {name}", f"import {name}-retry")
    return any(s in names or s.startswith(tuple(n + " (" for n in names)) for s in subjects)


def batch_state(batch: dict, subjects: set[str], referenced: set[str]) -> tuple[str, list[str]]:
    """(state, files to run). `done` needs both a commit for the batch and every one of its (non-gated)
    files listed in some page's `sources` now; the commit subject alone is only a hint. A batch that
    never committed runs whole (`todo`). A committed batch with files no page lists any more (undone,
    a run that exited 0 without writing pages, a failed run) runs again for those files only (`redo`)."""
    if not batch["files"]:
        return "done", []
    if batch.get("refresh"):  # named for this run (refresh_plan), so never run before
        return "todo", list(batch["files"])
    if not batch_committed(batch["name"], subjects):
        return "todo", list(batch["files"])
    left = [r for r in batch["files"] if r not in referenced]
    return ("redo", left) if left else ("done", [])


def plan_text(plan: list[dict], states: dict[str, tuple[str, list[str]]]) -> str:
    lines = [f"{len(plan)} batches, {sum(b['chars'] for b in plan) // 1024} KB, "
             f"{sum(len(b['files']) for b in plan)} files"]
    for b in plan:
        state, files = states[b["name"]]
        mark = f"redo {len(files)}/{len(b['files'])}" if state == "redo" else state
        lines.append(f"  {mark:<11} {b['tier']:<7} {b['name']:<28} {len(b['files']):>4} files "
                     f"{b['chars'] // 1024:>4} KB  " + ", ".join(p["hub"] for p in b["projects"]))
    todo = sum(b["chars"] if states[b["name"]][0] != "redo" else 0 for b in plan if states[b["name"]][0] != "done")
    if todo:
        lines.append(estimate_text(todo))
    return "\n".join(lines)


# Measured on one import of 1,583 memory files (4.2 MB) with an Opus-class model: 115 M tokens went
# through the model, 93% of them cached re-reads of the same context, for $63 at list prices.
TOKENS_PER_MB, USD_PER_MB = 27_000_000, 15.0


def estimate_text(chars: int) -> str:
    mb = chars / 1_000_000
    tokens = mb * TOKENS_PER_MB
    amount = f"{tokens / 1e6:.1f} M" if tokens >= 1e6 else f"{max(1, round(tokens / 1e3))} K"
    return (f"estimate for the {max(1, chars // 1024)} KB still to do: about {amount} tokens through "
            f"the model (mostly cached re-reads), about ${mb * USD_PER_MB:.2f} at Opus-class list prices; a smaller "
            "model costs a fraction, and a subscription runner draws on its quota instead")


# ---- model runs --------------------------------------------------------------------------------

INTEGRATE = threading.Lock()
# A provider quota or rate limit ends a failed run with an error line from omp such as
# `429 {"type":"error","error":{"type":"rate_limit_error",...}} retry-after-ms=2068000`. Only the log's
# last lines count, and only in these shapes, so a quoted 429 in model output does not stop imports.
LIMIT_LINE = re.compile(r'(?i)^(?:error:?\s*)?(?:402|429)\b|"status(?:Code)?"\s*:\s*(?:402|429)\b'
                        r'|"type"\s*:\s*"rate_limit_error"|\busage limit\b')
RETRY_AFTER = re.compile(r"(?i)retry-after(-ms)?\W{0,3}(\d+)")
LIMIT = threading.Event()
START = threading.Lock()  # held from the stop check through a model spawn, and while a limit is published
_LIMIT_AT: list[float] = []
_STOPPED: list[str] = []  # set by SIGTERM: start no more runs; running ones finish and get redacted


def stop_on_sigterm(signum, frame) -> None:
    """A caller's timeout (the nightly dream) sends SIGTERM to the process group: the model runs
    end, `run_model` still redacts their logs and sessions, and no new run starts."""
    _STOPPED.append("stopped by SIGTERM")
    LIMIT.set()


def check_limit(rc: int, log_file: Path) -> bool:
    """True (and LIMIT set) when a failed run ended on a provider 402/429; notes its retry-after.
    Callers hold START, so no run is spawned between the error and the stop."""
    if rc == 0 or not log_file.exists():
        return False
    tail = [line.strip() for line in log_file.read_text(errors="replace")[-20_000:].splitlines() if line.strip()]
    hits = [line for line in tail[-20:] if LIMIT_LINE.search(line)]
    if not hits:
        return False
    waits = [int(m.group(2)) / (1000 if m.group(1) else 1) for line in hits for m in RETRY_AFTER.finditer(line)]
    if waits:
        _LIMIT_AT.append(time.time() + waits[-1])
    LIMIT.set()
    return True


def limit_text() -> str:
    if _STOPPED:
        return "stopped on request (SIGTERM)"
    if not _LIMIT_AT:
        return "provider limit: retry later"
    return f"provider limit: retry after {datetime.fromtimestamp(max(_LIMIT_AT)):%H:%M}"


def render(template: str, **values: str) -> str:
    text = (CODE / "prompts" / template).read_text()
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def schema_text() -> str:
    f = WIKI / "SCHEMA.md"
    return (f if f.exists() else CODE / "schema.md").read_text().strip()


def prepare_work(name: str, inbox_files: list[str] = ()) -> tuple[Path, dict[str, str]]:
    """Scratch working copy: every non-gated page, the schema, the index, and the chosen inbox notes.
    Returns the work dir and the base hashes used for the three-way integration."""
    work = STATE / "work" / name
    if work.exists():
        shutil.rmtree(work)
    (work / "wiki/pages").mkdir(parents=True)
    (work / "wiki/inbox").mkdir()
    base = {}
    for p in load_pages().values():
        if p.gated:
            continue
        (work / "wiki/pages" / p.path.name).write_text(p.text)
        base[p.slug] = sha(p.text)
    for n in inbox_files:
        shutil.copy(INBOX / n, work / "wiki/inbox" / n)
    shutil.copy(WIKI / "SCHEMA.md", work / "wiki/SCHEMA.md")
    shutil.copy(WIKI / "index.md", work / "wiki/index.md")
    return work, base


def _spellings(p: Path) -> list[str]:
    """A path as written, with its parent resolved (a symlink itself), and fully resolved (its target)."""
    a = os.path.abspath(os.path.expanduser(str(p)))
    return list(dict.fromkeys([a, os.path.join(os.path.realpath(os.path.dirname(a)), os.path.basename(a)),
                               os.path.realpath(a)]))


def _ancestors(path: str) -> list[str]:
    out, d = [], os.path.dirname(path)
    while d not in ("", "/"):
        out.append(d)
        d = os.path.dirname(d)
    return out


def protected_paths() -> tuple[list[Path], list[Path]]:
    """(directories, files) a model run must not write: the live wiki (the CLI integrates and commits;
    the model edits only its scratch copy under the state dir), this CLI's code and config, and the
    instructions, skills and settings agent sessions load. Memory dirs are handled apart: see below."""
    claude, omp, codex = HOME / ".claude", HOME / ".omp/agent", HOME / ".codex"
    dirs = [WIKI, CODE, CONFIG, claude / "skills", claude / "agents", claude / "commands", claude / "hooks",
            omp / "skills", omp / "rules", omp / "agents", omp / "commands", omp / "extensions", codex / "skills"]
    files = [claude / "CLAUDE.md", claude / "settings.json", claude / "settings.local.json",
             omp / "AGENTS.md", codex / "AGENTS.md", codex / "config.toml", EXCLUDE_FILE]
    return dirs, files


def memory_dirs() -> list[Path]:
    return [d for root in dict.fromkeys((PROJECTS, HOME / ".claude/projects")) if root.is_dir()
            for d in root.glob("*/memory")]


def sandbox_profile() -> str:
    """macOS Seatbelt profile for model runs, which have every tool approved: everything is allowed
    except writes to `protected_paths()` and to any `<project>/memory` dir under the memory root (the
    runner may still write its own transcripts beside them).
    Every protected path is denied under each spelling (as written, symlink itself, symlink target),
    and each of their ancestor dirs is denied as a literal, so `~/.claude` cannot be renamed away,
    edited and moved back; files inside those ancestors stay writable."""
    subpaths, literals = protected_paths()
    roots = list(dict.fromkeys(s for p in (PROJECTS, HOME / ".claude/projects") for s in _spellings(p)))
    subs = list(dict.fromkeys(s for p in subpaths for s in _spellings(p)))
    lits = list(dict.fromkeys(s for p in [*literals, *map(Path, roots)] for s in _spellings(p)))
    state = os.path.realpath(STATE)
    ancestors = [a for a in dict.fromkeys(sp for s in subs + lits for a in _ancestors(s) for sp in _spellings(Path(a)))
                 if a not in lits and a != state and not a.startswith(state + "/")]
    rules = [f"(subpath {json.dumps(s)})" for s in subs] + [f"(literal {json.dumps(s)})" for s in lits + ancestors]
    # <root>/<project>/memory and everything in it; a runner's transcripts beside it stay writable
    rules += [f'(regex #"^{re.escape(r)}/[^/]+/memory(/.*)?$")' for r in roots]
    for d in memory_dirs():  # an existing project cannot be moved away and back, nor reached through a symlink
        rules += [f"(literal {json.dumps(sp)})" for sp in _spellings(d.parent)]
        if os.path.realpath(d) != str(d):
            rules.append(f"(subpath {json.dumps(os.path.realpath(d))})")
    return "(version 1)\n(allow default)\n(deny file-write*\n  " + "\n  ".join(rules) + ")\n"


def bwrap_args() -> list[str]:
    """Linux: bubblewrap with the whole filesystem writable except the protected paths, bound read-only."""
    dirs, files = protected_paths()
    memory = memory_dirs()
    args = ["--dev-bind", "/", "/"]
    for p in dict.fromkeys(os.path.realpath(x) for x in [*dirs, *files, *memory] if x.exists()):
        args += ["--ro-bind", p, p]
    state = os.path.realpath(STATE)  # the scratch copies live here and stay writable even under a protected dir
    return [*args, "--bind", state, state]


def sandbox_prefix() -> list[str]:
    """The sandbox a model run starts under: `sandbox-exec` on macOS, `bwrap` on Linux. Without one the
    run refuses to start; WIKI_SANDBOX=0 runs it unprotected."""
    if os.environ.get("WIKI_SANDBOX", "1") == "0":
        return []
    state = os.path.realpath(STATE)
    for p in (*protected_paths()[0], *memory_dirs()):
        real = os.path.realpath(p)
        if state == real or state.startswith(real + "/") or real.startswith(state + "/"):
            raise RuntimeError(f"WIKI_STATE ({state}) overlaps the protected path {real}: the scratch copies "
                               "would make it writable. Keep the state dir apart from the wiki, the memory "
                               "folders and this CLI's code")
    if sys.platform == "darwin":
        exe = shutil.which("sandbox-exec")
        if exe:
            return [exe, "-p", sandbox_profile()]
    else:
        exe = shutil.which("bwrap")
        if exe:
            return [exe, *bwrap_args()]
    raise RuntimeError("no sandbox found (sandbox-exec on macOS, bubblewrap on Linux): model runs are sandboxed; "
                       "install it, or set WIKI_SANDBOX=0 to run them without")


def redact_run(cwd: Path, t0: float, extra: list[Path] = ()) -> list[str]:
    """Logs, reports and the run's omp sessions stay on disk: redact them like wiki pages. A session
    belongs to the run when it changed since t0 and its header names the run's cwd."""
    paths = list(extra)
    where = os.path.realpath(cwd)
    for j in (STATE / "sessions").glob("*.jsonl"):
        try:
            if j.stat().st_mtime < t0:
                continue
        except OSError:
            continue
        hdr = session_header(j)[0]
        if not hdr or os.path.realpath(hdr) != where:
            continue
        paths.append(j)
        side = j.with_suffix("")  # subagent transcripts and tool logs
        if side.is_dir():
            paths += [f for f in side.rglob("*") if f.is_file()]
    return [str(p) for p in paths if p.is_file() and redact_file(p)]


def cmd_redact_since(args) -> int:
    """`wiki _redact-since <epoch>`: the fallback after a forced kill skipped `redact_run`. Redacts
    every log and session file under the state dir changed since then."""
    since = float(args.since)
    hits = [str(f) for d in (STATE / "logs", STATE / "sessions") if d.is_dir() for f in d.rglob("*")
            if f.is_file() and f.stat().st_mtime >= since and redact_file(f)]
    out(args, {"redacted": hits}, f"redacted {len(hits)} file(s)")
    return 0


def seconds(spec: str) -> int:
    """`50m`, `2h`, `900s` or a bare number of seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", spec)
    if not m:
        raise Usage(f"bad duration {spec!r}: use 900s, 50m or 2h")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def runner_command(prompt: str, cwd: Path, max_time: str) -> list[str]:
    """The headless agent command for one model run; it must be free to edit files under cwd."""
    model = ["--model", MODEL] if MODEL else []
    if RUNNER == "claude":
        return ["claude", "-p", prompt, "--dangerously-skip-permissions", *model]
    if RUNNER == "codex":
        return ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                "-C", str(cwd), *model, prompt]
    if RUNNER == "omp":
        return [*shlex.split(os.environ.get("WIKI_OMP", "omp")), "-p", *model, "--thinking", THINKING,
                "--approval-mode", "yolo", "--session-dir", str(STATE / "sessions"), "--cwd", str(cwd),
                "--max-time", max_time, prompt]
    if RUNNER == "custom" and RUNNER_CMD:
        return [a.replace("{prompt}", prompt).replace("{cwd}", str(cwd)).replace("{model}", MODEL) for a in RUNNER_CMD]
    raise Usage("no model runner: install Claude Code, Codex or omp, or set WIKI_RUNNER=claude|codex|omp, or "
                "WIKI_RUNNER_CMD to a command with {prompt}, {cwd} and {model}")


def run_model(prompt: str, cwd: Path, log_file: Path, max_time: str, extra: list[Path] = (),
              stoppable: bool = False) -> tuple[Optional[int], float]:
    """Run the model runner to completion; (exit code, seconds). A stoppable run checks LIMIT under
    START right before spawning and returns (None, 0) without starting when a provider limit came in.
    A run past max_time is stopped and counts as failed (exit 124)."""
    limit = seconds(max_time)  # a bad value fails here, before anything starts
    cmd = (["caffeinate", "-i"] if shutil.which("caffeinate") else []) + sandbox_prefix() + \
        runner_command(prompt, cwd, max_time)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    (STATE / "sessions").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with START if stoppable else contextlib.nullcontext():  # released once spawned, not when done
        if stoppable and LIMIT.is_set():
            return None, 0.0
        fh = log_file.open("w")
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        except BaseException:
            fh.close()
            raise
    with fh:
        try:
            rc = proc.wait(timeout=limit + 120)  # omp enforces max_time itself; this is the backstop
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            with contextlib.suppress(ProcessLookupError):  # children that ignored SIGTERM go too
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            rc = 124
    redact_run(cwd, t0 - 1, [log_file, *extra])
    return rc, time.time() - t0


def integrate(work: Path, base: dict[str, str], tag: str, inbox_given: list[str] = ()) -> dict:
    """Three-way merge of the scratch copy into the live wiki. A page is taken when the live page
    still matches the base the run started from (or both are new); otherwise the run's version goes
    to inbox/ for a serial merge pass, so parallel runs never overwrite each other."""
    live = load_pages()
    gated = {s for s, p in live.items() if p.gated}
    res = {"added": [], "updated": [], "deleted": [], "conflicts": [], "notes": [], "inboxDone": []}
    scratch = {f.stem: f.read_text(errors="replace") for f in sorted((work / "wiki/pages").glob("*.md"))}
    INBOX.mkdir(exist_ok=True)

    def to_inbox(slug: str, text: str, why: str) -> None:
        f = unique_note(slug, tag)
        f.write_text(f"<!-- conflict: batch {tag} {why}; fold its new facts and sources into "
                     f"pages/{slug}.md -->\n{text}")
        res["conflicts"].append(slug)

    for slug, text in scratch.items():
        b = base.get(slug)
        if b is not None and sha(text) == b:
            continue
        cur = live.get(slug)
        if slug in gated:
            to_inbox(slug, text, "wrote a page under a gated slug")
        elif (b is None and cur is None) or (b is not None and cur is not None and sha(cur.text) == b):
            page = Page(PAGES / f"{slug}.md", text)
            (PAGES / f"{slug}.md").write_text(normalized_text(page) if page.meta else text)
            res["added" if cur is None else "updated"].append(slug)
        elif cur is None:
            to_inbox(slug, text, "edited a page another run deleted")
        else:
            to_inbox(slug, text, "edited this page from an older copy" if b else "created a page another run created first")
    for slug, b in base.items():
        if slug not in scratch and slug in live and sha(live[slug].text) == b:
            live[slug].path.unlink()
            res["deleted"].append(slug)
    for note in sorted((work / "wiki/inbox").glob("*.md")):
        if note.name in inbox_given:
            continue
        f = unique_note(note.stem.split("--")[0], tag)
        shutil.copy(note, f)
        res["notes"].append(f.name)
    for n in inbox_given:  # a merge run deletes the notes it folded in
        if not (work / "wiki/inbox" / n).exists() and (INBOX / n).exists():
            (INBOX / n).unlink()
            res["inboxDone"].append(n)
    return res


def unique_note(target: str, tag: str) -> Path:
    """inbox/<target>--<tag>.md, or -2, -3 … when a note of that name is already waiting."""
    f, n = INBOX / f"{target}--{tag}.md", 2
    while f.exists():
        f, n = INBOX / f"{target}--{tag}-{n}.md", n + 1
    return f


def result_lines(res: dict) -> list[str]:
    lines = []
    for k in ("added", "updated", "deleted", "conflicts"):
        if res[k]:
            lines.append(f"{k} {len(res[k])}: " + ", ".join(res[k][:40]) + (" …" if len(res[k]) > 40 else ""))
    if res["notes"]:
        lines.append(f"inbox notes {len(res['notes'])}: " + ", ".join(res["notes"][:20]))
    if res["inboxDone"]:
        lines.append(f"inbox folded {len(res['inboxDone'])}")
    return lines or ["no page changes"]


def record_run(entry: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "runs.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def mode_text(tier: str) -> str:
    if tier == "refresh":
        return ("This is a refresh. Every source file above changed after the wiki took it in (or no page lists "
                "it yet): it supersedes what the pages say about it. Find the pages whose `sources` list each "
                "file (search `wiki/pages/` for its path) and update their facts in place: replace statements the "
                "file now contradicts, add its new facts next to related ones, keep each fact's existing date "
                "unless the file gives a newer one, and never add a second copy of a fact or a near-duplicate "
                "page. Keep the file in those pages' `sources`. Facts the file no longer states stay unless it "
                "says they are wrong or obsolete. You may edit any page in `wiki/pages/`, including global ones; "
                "other refresh batches may run on their own copies, and the CLI reconciles overlapping edits. Do "
                "not write to `wiki/inbox/`.")
    if tier == "global":
        return ("This batch runs alone. You may create and edit any page in `wiki/pages/`, including the "
                "global `user`, `feedback`, `machine` and `tool` pages. Prefer extending an existing page to "
                "creating a near-duplicate. Do not write to `wiki/inbox/`.")
    return ("Other batches run at the same time on their own copies. A later serial merge pass reconciles them, "
            "so stay in your lane:\n"
            "- Create and edit this batch's hub pages and pages scoped to this batch's projects.\n"
            "- Create new `tool` or `topic` pages (scope `global` if they apply across projects) when no page in "
            "`wiki/index.md` covers the subject.\n"
            "- Do not edit pages that already exist and belong to other projects or are global (`user`, "
            "`feedback`, `machine`, existing `tool`/`topic` pages). For facts that belong on one of those, write "
            "a note file `wiki/inbox/<target-slug>.md`: a bullet list of the facts with absolute dates, then a "
            "`sources:` list with the `<slug>/memory/<file>.md` paths they came from. A note can target a page "
            "that does not exist yet. Notes count as homes for their source files in the check.\n"
            "- Feedback rules that only apply to this batch's projects go in project-scoped `feedback` pages "
            "(`feedback-<project>-<topic>`), which you may create.")


def run_import_batch(batch: dict, n: int, total: int, retry: bool = True) -> Optional[dict]:
    """One model run for a batch, integrated and committed. None when a provider limit stopped imports."""
    if LIMIT.is_set():
        return None
    with INTEGRATE:
        # Re-read the memory dirs right before staging: files excluded or gated since the plan was made
        # never reach the model, and newly gated ones get their verbatim page now.
        projects = refresh_projects()
        mine_slugs = {p["slug"] for p in batch["projects"]}
        copy_gated([p for p in projects if p["slug"] in mine_slugs])
        batch, dropped = revalidate(batch, projects)
        if not batch["files"]:
            return {"batch": batch["name"], "rc": 0, "seconds": 0, "commit": None, "dropped": dropped, "left": [],
                    "added": [], "updated": [], "deleted": [], "conflicts": [], "notes": [], "inboxDone": []}
        work, base = prepare_work(batch["name"])
    staged_at = time.time()
    staged = {}  # rel -> sha256 of the bytes the run got (before redaction, so it compares with the file)
    for rel in batch["files"]:
        dst = work / "src" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = (PROJECTS / rel).read_bytes()
        dst.write_bytes(data)
        staged[rel] = hashlib.sha256(data).hexdigest()
    for p in batch["projects"]:
        idx = PROJECTS / p["slug"] / "memory/MEMORY.md"
        if idx.exists():
            shutil.copy(idx, work / "src" / p["slug"] / "memory/MEMORY.md")
    for f in (work / "src").rglob("*"):  # the model and a retained scratch dir see only redacted copies
        if f.is_file():
            redact_file(f)
    gated = [g for proj in projects if proj["slug"] in mine_slugs for g in proj["gated"]]
    gated_text = ("Gated files in these projects were copied into gated pages by the CLI. They are not in "
                  "`src/`; never open them and never write about their subjects: " +
                  ", ".join(f"`{g['rel']}`" for g in gated)) if gated else ""
    prompt = render(
        "import.md", BATCH=batch["name"], N=str(n), TOTAL=str(total), DATE=today(), WORK=str(work),
        SCHEMA=schema_text(), MODE=mode_text(batch["tier"]), GATED=gated_text,
        PROJECTS="\n".join(f"- `{p['cwd']}` → `{p['hub']}` (memory `{p['slug']}`)" for p in batch["projects"]),
        COUNT=str(len(batch["files"])), KB=str(batch["chars"] // 1024),
        FILES="\n".join(f"- `src/{r}`" for r in batch["files"]), CHECK=f"{SELF} _check {work}")
    log_file = STATE / "logs" / f"import-{batch['name']}.log"
    rc, secs = run_model(prompt, work, log_file, MAX_TIME, stoppable=True)
    if rc is None:  # a provider limit came in while this batch was being staged
        shutil.rmtree(work, ignore_errors=True)
        return None
    with START:
        limited = check_limit(rc, log_file)
    with INTEGRATE:
        res = integrate(work, base, batch["name"])
        referenced = referenced_sources()
        uncovered = [r for r in batch["files"] if r not in referenced]
        noted = covered_by_notes(work, uncovered)
        unchecked = set(check_work(work)["uncovered"])  # what `_check` says about the run's own copy
        covered = [r for r in batch["files"] if r not in unchecked and (r in referenced or r in noted)]
        status = "" if rc == 0 else f" (partial, exit {rc})"
        sha_ = None
        if not (limited and not any(res.values())):  # a refused run that changed nothing leaves no commit
            append_log("import", batch["name"] + status, [
                f"{len(batch['files'])} memory files, {batch['chars'] // 1024} KB, "
                f"projects: {', '.join(p['hub'] for p in batch['projects'])}",
                *([f"dropped from the stored plan (now excluded, gated or gone): {len(dropped)}"] if dropped else []),
                f"runner {RUN_LABEL}, {secs / 60:.1f} min, exit {rc}, log {log_file}",
                *result_lines(res),
                f"uncovered after batch: {len(uncovered) - len(noted)} (+{len(noted)} waiting in inbox notes)",
                *([f"check: {len(unchecked & set(batch['files']))} of the batch's files uncovered in its copy"]
                  if unchecked & set(batch["files"]) else [])])
            sha_ = commit(f"import {batch['name']}{status}")
        # A refresh is clean only with exit 0, a commit and every file covered: its files were listed
        # before it started, so coverage alone says nothing. Other batches are done per covered file.
        clean = rc == 0 and sha_ is not None and len(covered) == len(batch["files"])
        done = (batch["files"] if clean else []) if batch.get("refresh") else covered if sha_ else []
        if done:
            note_ingested({r: staged[r] for r in done}, staged_at)
    record_run({"kind": "import", "batch": batch["name"], "rc": rc, "seconds": round(secs), "commit": sha_,
                "files": len(batch["files"]), "date": datetime.now().isoformat(timespec="seconds")})
    print(f"[{datetime.now():%H:%M}] import {batch['name']}: exit {rc}, {secs / 60:.1f} min, "
          f"+{len(res['added'])} ~{len(res['updated'])} conflicts {len(res['conflicts'])} notes {len(res['notes'])}"
          + (f"; {limit_text()}, no new runs" if limited else ""), file=sys.stderr, flush=True)
    if rc == 0 or (limited and sha_ is None):
        shutil.rmtree(work, ignore_errors=True)
    if batch.get("refresh"):
        left = [] if clean else list(batch["files"])
    else:
        left = [r for r in batch["files"] if r not in covered]
    if retry and left and not LIMIT.is_set():
        # One more run for the files the run left without a home, or for a refresh that failed or did
        # not pass the check: provider stream errors happen, and a run can exit 0 without writing pages.
        sizes = {f["rel"]: f["chars"] for p in projects for f in p["files"]}
        again = {**batch, "name": f"{batch['name']}-retry", "files": left,
                 "chars": sum(sizes.get(r, 0) for r in left)}
        return run_import_batch(again, n, total, retry=False)
    return {"batch": batch["name"], "rc": rc, "seconds": round(secs), "commit": sha_, "dropped": dropped,
            "left": left, "limited": limited, **res}


def covered_by_notes(work: Path, rels: list[str]) -> list[str]:
    text = "\n".join(f.read_text(errors="replace") for f in (work / "wiki/inbox").glob("*.md"))
    return [r for r in rels if r in text]


_PROJECTS_CACHE: Optional[list[dict]] = None


def source_projects_cache() -> list[dict]:
    global _PROJECTS_CACHE
    if _PROJECTS_CACHE is None:
        _PROJECTS_CACHE = source_projects()
    return _PROJECTS_CACHE


def refresh_projects() -> list[dict]:
    global _PROJECTS_CACHE
    _PROJECTS_CACHE = source_projects()
    return _PROJECTS_CACHE


def run_merge_pass(name: str, inbox_files: list[str], lint_findings: str) -> Optional[dict]:
    if LIMIT.is_set():
        return None
    inbox_files = [n for n in inbox_files if note_gate(INBOX / n) is None]  # staging never takes a gated note
    with INTEGRATE:
        work, base = prepare_work(name, inbox_files)
    prompt = render("merge.md", BATCH=name, DATE=today(), WORK=str(work), SCHEMA=schema_text(),
                    INBOX="\n".join(f"- `wiki/inbox/{n}`" for n in inbox_files) or "- none",
                    LINT=lint_findings or "- none", CHECK=f"{SELF} _check {work}")
    log_file = STATE / "logs" / f"import-{name}.log"
    rc, secs = run_model(prompt, work, log_file, MAX_TIME, stoppable=True)
    if rc is None:
        shutil.rmtree(work, ignore_errors=True)
        return None
    with START:
        limited = check_limit(rc, log_file)
    with INTEGRATE:
        res = integrate(work, base, name, inbox_files)
        status = "" if rc == 0 else f" (partial, exit {rc})"
        sha_ = None
        if not (limited and not any(res.values())):
            append_log("import", name + status, [f"{len(inbox_files)} inbox notes given; runner {RUN_LABEL}, "
                                                 f"{secs / 60:.1f} min, exit {rc}, log {log_file}",
                                                 *result_lines(res)])
            sha_ = commit(f"import {name}{status}")
    record_run({"kind": "merge", "batch": name, "rc": rc, "seconds": round(secs), "commit": sha_,
                "date": datetime.now().isoformat(timespec="seconds")})
    print(f"[{datetime.now():%H:%M}] {name}: exit {rc}, {secs / 60:.1f} min, folded {len(res['inboxDone'])}"
          f"/{len(inbox_files)}", file=sys.stderr, flush=True)
    if rc == 0:
        shutil.rmtree(work, ignore_errors=True)
    return {"batch": name, "rc": rc, "seconds": round(secs), "commit": sha_, "limited": limited, **res}


def next_name(prefix: str) -> str:
    n = sum(1 for s in committed_subjects() if s.startswith(f"import {prefix}-")) + 1
    return f"{prefix}-{n:02d}"


def merge_rounds(max_rounds: int = 6) -> list[dict]:
    """Fold inbox notes serially, grouped by target page, in chunks that fit one run."""
    results = []
    for _ in range(max_rounds):
        folded = fold_gated_notes()
        if folded:
            append_log("fold", "gated notes", [f"{len(folded)} gated notes placed verbatim, no model"])
            commit("fold gated notes")
        # a gated note never reaches a model: each listed file is checked itself, so one saved just now
        # is either absent from this round or seen as gated
        notes = [f for f in sorted(INBOX.glob("*.md"), key=lambda f: f.name) if note_gate(f) is None]
        if not notes:
            break
        chunks, cur, size, last_target = [], [], 0, None
        for f in notes:
            # an agent note stands alone; other notes stay with the notes for the same page
            target = f.name if f.name.startswith(NOTE_PREFIX) else f.name.split("--")[0]
            sz = f.stat().st_size
            if cur and size + sz > BATCH_CHARS * 0.7 and target != last_target:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(f.name)
            size += sz
            last_target = target
        if cur:
            chunks.append(cur)
        progressed = False
        for i, names in enumerate(chunks):
            findings = lint_text(lint(load_pages()), limit=150) if i == len(chunks) - 1 else ""
            r = run_merge_pass(next_name("merge"), names, findings)
            if r is None:
                break
            results.append(r)
            progressed |= bool(r["inboxDone"])
        if not progressed or LIMIT.is_set():
            break
    return results


def gap_plan(projects: list[dict], missing: list[str]) -> list[dict]:
    miss = set(missing)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    parts = [(p, [f for f in p["files"] if f["rel"] in miss]) for p in projects]
    parts = [(p, fs) for p, fs in parts if fs]
    plan, cur, size = [], [], 0
    for p, fs in parts:
        for piece in chunk(fs, BATCH_CHARS):
            sz = sum(f["chars"] for f in piece)
            if cur and size + sz > BATCH_CHARS:
                plan.append(cur)
                cur, size = [], 0
            cur.append((p, piece))
            size += sz
    if cur:
        plan.append(cur)
    return [make_batch(f"gaps-{stamp}-{i:02d}", "global", members, "gaps") for i, members in enumerate(plan, 1)]


# ---- verbs -------------------------------------------------------------------------------------

def cmd_init(args) -> int:
    with Lock():
        created = init_repo()
    out(args, {"home": str(WIKI), "created": created}, f"{'created' if created else 'exists'}: {WIKI}")
    return 0


def cmd_status(args) -> int:
    ensure_repo()
    pages = load_pages()
    counts: dict[str, int] = {}
    for p in pages.values():
        k = "gated" if p.gated else (p.kind or "?")
        counts[k] = counts.get(k, 0) + 1
    dirty = [line for line in git("status", "--porcelain").stdout.splitlines()]
    res = lint(pages)
    data = {"home": str(WIKI), "state": str(STATE), "pages": len(pages), "byKind": counts,
            "lastImport": last_commit("^import "), "lastDream": last_commit("^dream "),
            "head": last_commit("."), "dirty": dirty, "lint": {k: (len(v) if isinstance(v, list) else v)
                                                              for k, v in res.items()},
            "spend": spend()}
    li, ld = data["lastImport"], data["lastDream"]
    text = [f"wiki {WIKI}: {len(pages)} pages (" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ")",
            f"last import: {li['subject']} {li['date'][:16]} {li['commit']}" if li else "last import: never",
            f"last dream:  {ld['subject']} {ld['date'][:16]} {ld['commit']}" if ld else "last dream:  never",
            f"git: {'clean' if not dirty else f'{len(dirty)} uncommitted changes'}",
            "lint: " + lint_summary(res),
            "model runs: {runs} ({failed} failed), {hours} h of run time".format(**data["spend"])
            + (", {tokens:,} tokens, ${usd} at list prices".format(**data["spend"]) if data["spend"]["tokens"] else "")]
    out(args, data, "\n".join(text))
    return 0


def spend() -> dict:
    """Model runs from runs.jsonl. Tokens and list-price cost are known only for the omp runner, which
    records them in its session files (subagents included)."""
    runs = [json.loads(line) for line in (STATE / "runs.jsonl").read_text().splitlines()] \
        if (STATE / "runs.jsonl").exists() else []
    usd, tokens = 0.0, 0
    for f in (STATE / "sessions").rglob("*.jsonl") if (STATE / "sessions").exists() else []:
        for line in f.open(errors="replace"):
            if '"usage"' not in line:
                continue
            try:
                usage = (json.loads(line).get("message") or {}).get("usage") or {}
            except ValueError:
                continue
            usd += float((usage.get("cost") or {}).get("total") or 0)
            tokens += int(usage.get("totalTokens") or 0)
    return {"runs": len(runs), "failed": sum(1 for r in runs if r.get("rc")),
            "hours": round(sum(r.get("seconds", 0) for r in runs) / 3600, 2), "tokens": tokens,
            "usd": round(usd, 2)}


def cmd_index(args) -> int:
    ensure_repo()
    pages = load_pages()
    cwd = resolve_cwd(args.cwd)
    if cwd is None:
        text = render_index(pages)
        out(args, {"pages": [{"slug": p.slug, "kind": p.kind, "summary": p.summary, "gated": p.gated or None}
                             for p in pages.values()]}, text.rstrip())
        return 0
    sections = scoped_index(pages, cwd)
    hubs = [h.slug for h in hubs_for(pages, cwd)]
    text = f"# Wiki index for {cwd}\n" + "".join(f"\n## {h}\n\n" + "\n".join(lines) + "\n" for h, lines in sections)
    out(args, {"cwd": cwd, "hubs": hubs, "sections": [{"heading": h, "lines": lines} for h, lines in sections]},
        text.rstrip())
    return 0


def cmd_context(args) -> int:
    ensure_repo()
    if args.budget_chars < 1:
        raise Usage("--budget-chars must be at least 1")
    data = context_with_notes(resolve_cwd(args.cwd or os.getcwd()), args.budget_chars)
    if args.instructions:  # how to recall and save, for a session-start hook; on top of the budget
        data["text"] = ((CODE / "prompts/agent.md").read_text().rstrip() + "\n\n" + data["text"]).rstrip()
        data["chars"] = len(data["text"])
    out(args, data, data["text"].rstrip())
    return 0


def cmd_read(args) -> int:
    ensure_repo()
    pages = load_pages()
    missing = [s for s in args.slugs if link_target(s) not in pages]
    if missing:
        raise Usage(f"no such page: {', '.join(missing)}")
    sel = [pages[link_target(s)] for s in args.slugs]
    out(args, [{"slug": p.slug, "meta": p.meta, "body": p.body, "gated": p.gated or None} for p in sel],
        "\n\n".join((f"<!-- GATED: DO NOT READ UNLESS {p.gated} -->\n" if p.gated else "")
                    + f"<!-- pages/{p.slug}.md -->\n" + p.text.rstrip() for p in sel))
    return 0


def search(terms: list[str], cwd: Optional[str], gated: bool, limit: int) -> list[dict]:
    terms = [t.lower() for t in terms if t.strip()]
    phrase = " ".join(terms)
    hits = []
    for p in load_pages().values():
        if (p.gated and not gated) or not in_scope(p, cwd):
            continue
        title, summary, body = p.title.lower(), p.summary.lower(), p.body.lower()
        tags = " ".join(p.tags)
        matched, score = 0, 0
        for t in terms:
            c = (10 * title.count(t) + 6 * summary.count(t) + 5 * (t in tags) + 4 * (t in p.slug)
                 + min(body.count(t), 12))
            matched += c > 0
            score += c
        if not matched:
            continue
        if len(terms) > 1 and phrase in f"{title}\n{summary}\n{body}":
            score += 15
        snippet = next((" ".join(line.split())[:180] for line in p.body.splitlines()
                        if any(t in line.lower() for t in terms)), p.summary)
        hits.append({"slug": p.slug, "kind": p.kind, "matched": matched, "score": score, "summary": p.summary,
                     "snippet": snippet, "gated": p.gated or None})
    hits.sort(key=lambda h: (-h["matched"], -h["score"], h["slug"]))
    return hits[:limit]


def cmd_search(args) -> int:
    ensure_repo()
    hits = search(args.terms, resolve_cwd(args.cwd), args.gated, args.n)
    out(args, {"terms": args.terms, "hits": hits},
        "\n".join(f"{h['score']:>4} [[{h['slug']}]] ({h['kind']}) — {h['summary']}\n       {h['snippet']}"
                  for h in hits) or "no hits")
    return 0 if hits else 1


def cmd_backlinks(args) -> int:
    ensure_repo()
    slug = link_target(args.slug)
    pages = load_pages()
    if slug not in pages:
        raise Usage(f"no such page: {slug}")
    back = sorted(p.slug for p in pages.values() if slug in p.links() and p.slug != slug)
    out(args, {"slug": slug, "backlinks": back}, "\n".join(f"[[{s}]] — {pages[s].summary}" for s in back) or "none")
    return 0


def cmd_log(args) -> int:
    ensure_repo()
    entries, cur = [], None
    for line in (WIKI / "log.md").read_text().splitlines():
        m = LOG_HEAD.match(line)
        if m:
            cur = {"date": m.group(1), "op": m.group(2), "title": m.group(3), "lines": []}
            entries.append(cur)
        elif cur is not None and line.startswith("- "):
            cur["lines"].append(line[2:])
    sel = entries[-args.n:][::-1]
    out(args, sel, "\n".join(f"[{e['date']}] {e['op']} | {e['title']}" + "".join(f"\n    {x}" for x in e["lines"])
                             for e in sel))
    return 0


def cmd_lint(args) -> int:
    ensure_repo()
    if args.fix:
        with Lock():
            changed = fix_pages()
            reindexed = write_index()
            if changed or reindexed:
                append_log("lint", "fix", [f"normalized frontmatter: {len(changed)} pages"
                                           + (f" ({', '.join(changed[:30])})" if changed else ""),
                                           f"index regenerated: {'yes' if reindexed else 'no change'}"])
            sha_ = commit("lint --fix")
    res = lint(load_pages())
    if args.fix:
        res["fixed"] = {"pages": changed, "index": reindexed, "commit": sha_}
    out(args, res, lint_text(res) + (f"\nfixed: {len(changed)} pages normalized, index "
                                     f"{'regenerated' if reindexed else 'unchanged'}" if args.fix else ""))
    return 3 if res["issues"] else 0


def resolve_ref(ref: str) -> str:
    if ref == "last":
        ref = "HEAD"
    r = git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    if r.returncode:
        raise Usage(f"unknown ref {ref!r}")
    return r.stdout.strip()


def cmd_history(args) -> int:
    ensure_repo()
    slug = link_target(args.slug)
    r = git("log", f"-{args.n}", "--format=%H%x09%cI%x09%s", "--", f"pages/{slug}.md")
    rows = [dict(zip(("commit", "date", "subject"), line.split("\t", 2))) for line in r.stdout.splitlines()]
    if not rows:
        raise Usage(f"no history for pages/{slug}.md")
    out(args, {"slug": slug, "commits": rows},
        "\n".join([f"pages/{slug}.md"] + [f"  {c['commit'][:7]} {c['date'][:16]} {c['subject']}" for c in rows]))
    return 0


def cmd_diff(args) -> int:
    ensure_repo()
    if args.ref is None and git("status", "--porcelain").stdout.strip():
        stat = git("status", "--short").stdout
        patch = git("diff", "HEAD").stdout
        out(args, {"ref": None, "status": stat, "patch": patch}, (stat + "\n" + patch).rstrip())
        return 0
    sha_ = resolve_ref(args.ref or "HEAD")
    info = git("show", "-s", "--format=%h %cI %s", sha_).stdout.strip()
    stat = git("show", "--stat", "--format=", sha_).stdout
    patch = git("show", "--format=", sha_).stdout
    out(args, {"ref": sha_, "commit": info, "stat": stat, "patch": patch}, f"{info}\n{stat}\n{patch}".rstrip())
    return 0


def plan_undo(sha_: str) -> dict:
    if git("rev-parse", "--verify", "--quiet", sha_ + "^", check=False).returncode:
        raise Usage("refusing to undo the root commit")
    rows = git("diff-tree", "--no-commit-id", "--name-status", "-r", "--no-renames", sha_).stdout.splitlines()
    files, conflicts = [], []
    for row in rows:
        status, path = row.split("\t", 1)
        if path in ("index.md", "log.md"):
            continue  # generated / append-only: regenerated and appended instead
        at = git("rev-parse", "--verify", "--quiet", f"{sha_}:{path}", check=False).stdout.strip() or None
        f = WIKI / path
        cur = git("hash-object", str(f)).stdout.strip() if f.is_file() else None
        if cur != at:
            conflicts.append({"path": path, "reason": "changed after this commit; see `wiki history`"})
            continue
        before = git("rev-parse", "--verify", "--quiet", f"{sha_}^:{path}", check=False).stdout.strip() or None
        files.append({"path": path, "action": {"A": "delete", "D": "recreate"}.get(status[:1], "revert"),
                      "blob": before})
    return {"commit": sha_, "files": files, "conflicts": conflicts}


def cmd_undo(args) -> int:
    ensure_repo()
    sha_ = resolve_ref(args.ref)
    subject = git("show", "-s", "--format=%s", sha_).stdout.strip()
    plan = plan_undo(sha_)
    head = f"undo {sha_[:7]} ({subject})"
    lines = [f"  {f['action']:<8} {f['path']}" for f in plan["files"]] + \
            [f"  CONFLICT {c['path']}: {c['reason']}" for c in plan["conflicts"]]
    data = {"undo": sha_, "subject": subject, "dryRun": args.dry_run, **plan, "result": None}
    if args.dry_run or not plan["files"]:
        out(args, data, "\n".join([head + (" [dry run]" if args.dry_run else ": nothing to do"), *lines]))
        return 3 if plan["conflicts"] else 0
    with Lock():
        plan = plan_undo(sha_)
        for f in plan["files"]:
            dst = WIKI / f["path"]
            if f["blob"] is None:
                dst.unlink(missing_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(git_bytes("cat-file", "blob", f["blob"]) or b"")
        append_log("undo", f"{sha_[:7]} {subject}", [f"{f['action']} {f['path']}" for f in plan["files"]]
                   + [f"conflict (kept) {c['path']}" for c in plan["conflicts"]])
        new = commit(f"undo {sha_[:7]}: {subject}")
    data.update(plan, result={"commit": new})
    out(args, data, "\n".join([head, *lines, f"commit {new[:7] if new else '(none)'}"]))
    return 3 if plan["conflicts"] else 0


def check_work(work: Path) -> dict:
    """`_check` on a scratch dir: {problems, uncovered (staged sources no page or note in it lists)}.
    Import batches call it in-process after a run; the model runs it through `wiki _check`."""
    pages = load_pages(work / "wiki/pages")
    live_gated = {s for s, p in load_pages().items() if p.gated}
    problems, uncovered = [], []
    src = sorted(str(f.relative_to(work / "src")) for f in (work / "src").rglob("*.md")
                 if f.name != "MEMORY.md") if (work / "src").exists() else []
    referenced = {s for p in pages.values() for s in p.sources}
    notes = "\n".join(f.read_text(errors="replace") for f in (work / "wiki/inbox").glob("*.md"))
    for rel in src:
        if rel not in referenced and rel not in notes:
            uncovered.append(rel)
            problems.append(f"uncovered source: {rel} (add it to the `sources` of the page holding its facts)")
    for p in pages.values():
        for e in page_errors(p):
            problems.append(f"frontmatter {p.slug}: {e}")
        for t in p.links():
            if t not in pages and t not in live_gated:
                problems.append(f"dangling link [[{t}]] in {p.slug}")
        if len(p.text) > OVERSIZE:
            problems.append(f"oversize {p.slug}: {len(p.text)} chars (split it)")
    return {"problems": problems, "uncovered": uncovered}


def cmd_check(args) -> int:
    """Hidden verb the model runs inside its scratch dir: coverage, frontmatter, links, size."""
    problems = check_work(Path(args.work).resolve())["problems"]
    out(args, {"ok": not problems, "problems": problems}, "\n".join(problems) or "ok")
    return 1 if problems else 0


def select_plan(args, projects: list[dict], save: bool) -> list[dict]:
    if args.refresh or args.refresh_project or args.refresh_all:
        return refresh_plan(projects, refresh_targets(args, projects, save)[0])
    if args.project:
        return project_plan(projects, resolve_cwd(args.project))
    if args.gaps:
        return gap_plan(projects, coverage(projects)["missing"])
    plan_file = STATE / "import-plan.json"
    if plan_file.exists() and not args.replan:
        return json.loads(plan_file.read_text())
    plan = make_plan(projects)
    if save:
        STATE.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(plan, indent=2))
    return plan


def plan_states(plan: list[dict], projects: list[dict]) -> tuple[list[dict], dict, list[str]]:
    """The plan revalidated against the memory dirs now, each batch's state, and the dropped files."""
    subjects = committed_subjects() if (WIKI / ".git").exists() else set()
    # Sources named in inbox notes have a home waiting for this invocation's merge pass.
    notes = "\n".join(f.read_text(errors="replace") for f in INBOX.glob("*.md")) if INBOX.exists() else ""
    referenced = referenced_sources() | {r for b in plan for r in b["files"] if r in notes}
    checked, states, dropped = [], {}, []
    for b in plan:
        b, gone = revalidate(b, projects)
        checked.append(b)
        dropped += gone
        states[b["name"]] = batch_state(b, subjects, referenced)
    return checked, states, dropped


def cmd_import(args) -> int:
    if not (args.report or args.dry_run or args.mark_ingested):
        model_host_check()
    if args.report:
        ensure_repo()
        cov = coverage()
        STATE.joinpath("reports").mkdir(parents=True, exist_ok=True)
        md = STATE / "reports/import-coverage.md"
        md.write_text(SECRET.sub("[REDACTED-SECRET]",
                                 f"# Import coverage ({today()})\n\n{cov['covered']}/{cov['files']} non-gated memory "
                                 f"files ({cov['pct']}%) are listed in some page's `sources`; gated "
                                 f"{cov['gatedCovered']}/{cov['gated']}.\n\n## Not referenced\n\n"
                                 + "".join(f"- `{m}`\n" for m in cov["missing"])))
        (STATE / "reports/import-coverage.json").write_text(SECRET.sub("[REDACTED-SECRET]", json.dumps(cov, indent=2)))
        out(args, {**cov, "report": str(md)},
            f"coverage {cov['covered']}/{cov['files']} ({cov['pct']}%), gated {cov['gatedCovered']}/{cov['gated']}; "
            f"{len(cov['missing'])} missing; report {md}" + "".join(f"\n  {m}" for m in cov["missing"][:50]))
        return 0
    if args.mark_ingested:  # no model: take the memory files pages list now as what the wiki holds
        ensure_repo()
        with contextlib.nullcontext() if args.dry_run else Lock():
            known = {f["rel"] for p in source_projects() for f in p["files"] + p["gated"]}
            listed = sorted(referenced_sources() & known)
            if not args.dry_run:
                note_ingested({r: file_sha256(PROJECTS / r) for r in listed}, time.time())
        verb = "would mark" if args.dry_run else "marked"
        out(args, {"marked": 0 if args.dry_run else len(listed), "listed": len(listed),
                   "dryRun": args.dry_run, "ledger": str(STATE / "ingested.json")},
            f"{verb} {len(listed)} listed memory files as ingested in {STATE / 'ingested.json'}")
        return 0
    if not (args.all or args.project or args.gaps or args.refresh or args.refresh_project or args.refresh_all):
        raise Usage("pass --all, --project CWD, --gaps, --refresh PATH..., --refresh-project CWD, --refresh-all, "
                    "--mark-ingested or --report")
    if args.dry_run:  # reads only and saves nothing, so it takes no lock
        projects = source_projects_cache()
        plan, states, dropped = plan_states(select_plan(args, projects, save=False), projects)
        sample = next((states[b["name"]][1] for b in plan if states[b["name"]][0] != "done"), None)
        data = {"plan": plan, "states": {n: {"state": s, "files": fs} for n, (s, fs) in states.items()},
                "dropped": dropped}
        out(args, data, plan_text(plan, states)
            + (f"\ndropped from the stored plan (now excluded, gated or gone): {len(dropped)} files" if dropped else "")
            + (f"\nfirst prompt: {len(sample)} files; run without --dry-run to start" if sample else ""))
        return 0
    try:
        lock = Lock().__enter__()
    except Locked:
        out(args, {"error": "locked"}, "another import/dream/undo holds the lock")
        return 4
    t0 = time.time()
    try:
        init_repo()
        projects = refresh_projects()
        plan = select_plan(args, projects, save=True)
        if args.project or args.refresh_project:
            mine = {find_project(projects, resolve_cwd(args.project or args.refresh_project))["slug"]}
        elif args.refresh:
            mine = {source_rel(x).split("/", 1)[0] for x in args.refresh}
        else:
            mine = None
        gated = copy_gated([p for p in projects if mine is None or p["slug"] in mine])
        if gated:
            append_log("import", "gated", [f"copied verbatim without a model: {', '.join(gated)}"])
        commit("import gated")
        plan, states, _ = plan_states(plan, projects)
        sizes = {f["rel"]: f["chars"] for p in projects for f in p["files"]}
        todo = [{**b, "files": states[b["name"]][1], "chars": sum(sizes[r] for r in states[b["name"]][1])}
                for b in plan if states[b["name"]][0] != "done"]
        total = len(plan)
        pos = {b["name"]: i for i, b in enumerate(plan, 1)}
        results = []
        for b in [b for b in todo if b["tier"] == "global"]:
            results.append(run_import_batch(b, pos[b["name"]], total))
        chains: dict[str, list[dict]] = {}
        for b in todo:
            if b["tier"] != "global":
                chains.setdefault(b["chain"], []).append(b)

        def run_chain(bs: list[dict]) -> list[dict]:
            return [run_import_batch(b, pos[b["name"]], total) for b in bs]

        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
            for rs in ex.map(run_chain, sorted(chains.values(), key=lambda bs: -sum(b["chars"] for b in bs))):
                results += rs
        results = [r for r in results if r is not None]  # None: not started after a provider limit
        if not LIMIT.is_set():
            results += merge_rounds()
        res = lint(load_pages())
        if not LIMIT.is_set() and (res["dangling"] or res["orphans"] or res["duplicateTitles"] or res["oversize"]):
            if len(res["dangling"]) + len(res["orphans"]) + len(res["duplicateTitles"]) + len(res["oversize"]) >= 5:
                results += [r for r in [run_merge_pass(next_name("repair"), [], lint_text(res, limit=200))] if r]
        changed = fix_pages()
        if changed:
            append_log("lint", "fix after import", [f"normalized frontmatter: {len(changed)} pages"])
        commit("lint --fix")
        cov = coverage(projects)
        mins = (time.time() - t0) / 60
        append_log("import", "summary", [f"{len(results)} model runs this invocation, {mins:.0f} min wall",
                                         f"coverage {cov['covered']}/{cov['files']} ({cov['pct']}%)",
                                         lint_summary(lint(load_pages())),
                                         *([f"{limit_text()}; stopped starting runs"] if LIMIT.is_set() else [])])
        commit("import summary")
    finally:
        lock.__exit__(None, None, None)
    data = {"runs": len(results), "minutes": round(mins, 1), "coverage": cov["pct"],
            "failed": [r["batch"] for r in results if r["rc"] != 0 or r.get("left")], "results": results,
            "providerLimit": limit_text() if LIMIT.is_set() else None}
    out(args, data, (f"{limit_text()}\n" if LIMIT.is_set() else "")
        + f"{len(results)} model runs, {mins:.0f} min; coverage {cov['pct']}%; failed: "
          f"{', '.join(data['failed']) or 'none'}")
    return 5 if LIMIT.is_set() else 1 if data["failed"] else 0


# ---- dream -------------------------------------------------------------------------------------

def recent_sessions(since: float, dirs: tuple[str, ...]) -> list[dict]:
    found = []
    # Top-level sessions only: omp and Claude Code keep subagent transcripts one directory deeper.
    sources = [("omp", p) for p in OMP_SESSIONS.glob("*/*.jsonl")] + \
              [("claude", p) for p in PROJECTS.glob("*/*.jsonl")]
    for agent, p in sources:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < since or st.st_size < 2048:
            continue
        cwd, title = session_header(p)
        if not cwd or cwd.startswith(SKIP_CWD) or is_excluded(cwd, dirs):
            continue
        if str(STATE) in str(p) or cwd.startswith(str(STATE)):
            continue  # the wiki's own import/dream runs
        found.append({"path": str(p), "agent": agent, "cwd": cwd, "title": title,
                      "kb": st.st_size // 1024, "mtime": st.st_mtime})
    return sorted(found, key=lambda s: (s["cwd"], s["mtime"]))


def dream_manifest(since: float) -> tuple[str, list[dict]]:
    dirs = excluded_dirs()
    sessions = recent_sessions(since, dirs)
    pages = load_pages()
    parts = [f"omp and Claude Code sessions written since {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} "
             f"({len(sessions)} top-level; /tmp smoke sessions excluded):"]
    cwd = None
    for s in sessions:
        if s["cwd"] != cwd:
            cwd = s["cwd"]
            hubs = hubs_for(pages, cwd)
            parts.append(f"\n### {cwd}\nhub: " + (f"`{hubs[0].slug}`" if hubs else "none yet (create one if the "
                                                   "project has durable facts)"))
        when = datetime.fromtimestamp(s["mtime"]).strftime("%m-%d %H:%M")
        parts.append(f"- {when} {s['agent']} {s['kb']} KB \"{s['title']}\" `{s['path']}`")
    if dirs:
        parts += ["", "Off limits: never read sessions or memory under these directories, and skip any search "
                  "hit from them:", *(f"- `{d}`" for d in dirs)]
    return "\n".join(parts), sessions


def cmd_note(args) -> int:
    """Save one fact for later folding. No model, no git: an atomic file write that is safe in parallel."""
    ensure_repo()
    if args.type not in NOTE_TYPES:
        raise Usage(f"--type must be one of {', '.join(NOTE_TYPES)}")
    body = args.body if args.body is not None else sys.stdin.read()
    body = SECRET.sub("[REDACTED-SECRET]", body).strip()
    title = SECRET.sub("[REDACTED-SECRET]", " ".join(args.title.split()))
    if not title or not body:
        raise Usage("a note needs --title and a body (--body TEXT or stdin)")
    cwd = resolve_cwd(args.cwd or os.getcwd()) or ""
    if re.search(r"[\x00-\x1f\x7f]", cwd):
        raise Usage("--cwd holds a control character")
    host = socket.gethostname().split(".")[0].lower() or "host"
    now = datetime.now().astimezone()
    meta = {"title": title, "type": args.type, "cwd": cwd, "host": host,
            "created": now.isoformat(timespec="seconds")}
    if args.gated is not None:  # one line, so the frontmatter cannot lose the gate
        trig = re.sub(r"(?i)^do not read unless\s+", "", " ".join(args.gated.split()))
        trig = SECRET.sub("[REDACTED-SECRET]", trig)
        if not trig:
            raise Usage("--gated needs the trigger sentence")
        meta["gated"] = trig
    INBOX.mkdir(exist_ok=True)
    name = f"{NOTE_PREFIX}{now:%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9-]', '-', host)}-{os.urandom(3).hex()}.md"
    if args.page:
        slug = link_target(args.page)
        target = PAGES / f"{slug}.md"
        if args.gated is not None:
            raise Usage("--page and --gated do not combine: a gated note gets its own gated page")

        def open_page() -> Page:
            if not SLUG_OK.match(slug) or not target.is_file() or Page(target).gated:
                raise Usage(f"--page {args.page}: no such open page (see `wiki search` or `wiki index`)")
            return Page(target)

        open_page()
        try:
            if os.environ.get("WIKI_NOTES_ONLY") == "1":
                raise Locked()  # this host never edits pages: the note waits for the host that owns them
            with Lock():  # no model: the fact goes on the page now, verbatim
                page = open_page()  # again under the lock: a pass may have gated or removed it
                pm = dict(page.meta)
                pm["sources"] = list(dict.fromkeys([*page.sources, f"note/{name}"]))
                pm["updated"] = today()
                entry = f"\n## {title} ({now:%Y-%m-%d})\n\n{body}\n"
                tmp = WIKI / ".git" / f"page-{os.getpid()}-{os.urandom(3).hex()}.tmp"
                tmp.write_text(dump_frontmatter(pm) + page.body.rstrip("\n") + "\n" + entry)
                tmp.replace(target)
                sha_ = commit(f"note on {slug}: {title[:60]}")
            out(args, {"page": slug, "commit": sha_, **meta}, f"added to [[{slug}]]")
            return 0
        except Locked:  # a model pass is running: the note waits in the inbox and names its page
            meta["page"] = slug
    text = "---\n" + "".join(f"{k}: {_quote(v)}\n" for k, v in meta.items()) + "---\n" + body + "\n"
    text = SECRET.sub("[REDACTED-SECRET]", text)
    # Written inside .git (same filesystem, never staged), then renamed: a `git add -A` running now
    # sees no note or the whole note.
    tmp = WIKI / ".git" / f"note-{os.getpid()}-{os.urandom(3).hex()}.tmp"
    tmp.write_text(text)
    tmp.replace(INBOX / name)
    out(args, {"note": str(INBOX / name), **meta}, f"saved {INBOX / name}")
    return 0


def cmd_notes(args) -> int:
    ensure_repo()
    cwd = resolve_cwd(args.cwd) if args.cwd else None
    notes = [n if not n["gated"] else {**n, "body": None}  # a gated body is read from its page after a fold
             for n in pending_notes() if args.all or note_in_scope(n, cwd)]
    text = "\n".join(f"{n['created'][:16]}  {n['type']:<9} {n['host']:<10} {n['title']}" for n in notes) \
        or "no pending notes"
    out(args, notes, text)
    return 0


def cmd_sync(args) -> int:
    """Commit local notes, pull the shared wiki, push. Skips (exit 4) while a model pass holds the lock."""
    ensure_repo()
    if not git("remote", check=False).stdout.split():
        raise Usage("no git remote in the wiki repo; add one with `git -C <wiki> remote add origin <url>`")
    with Lock():
        host = socket.gethostname().split(".")[0].lower()
        if any((WIKI / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply")):
            # A sync that was killed mid-pull left its rebase open. Close it before committing: the abort
            # returns to the branch as it was, and notes saved since are untracked files it leaves alone.
            git("rebase", "--abort", check=False)
        sha_ = commit(f"notes from {host}")
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        pulled = git("pull", "--rebase", "-q", "origin", branch, check=False)
        if pulled.returncode != 0:
            git("rebase", "--abort", check=False)
            raise RuntimeError(f"pull failed: {pulled.stderr.strip() or pulled.stdout.strip()}")
        if write_index():
            commit("index refresh after sync")
        pushed = git("push", "-q", "origin", f"HEAD:{branch}", check=False)
        if pushed.returncode != 0:
            raise RuntimeError(f"push failed: {pushed.stderr.strip()}")
        head = git("rev-parse", "--short", "HEAD").stdout.strip()
    out(args, {"committed": sha_, "head": head, "pendingNotes": len(pending_notes())},
        f"synced at {head}" + (f" (committed {sha_[:7]})" if sha_ else "") + f"; {len(pending_notes())} pending notes")
    return 0


def cmd_fold(args) -> int:
    """(model) Fold pending agent notes and other inbox notes into pages."""
    ensure_repo()
    model_host_check()
    notes = sorted(INBOX.glob("*.md"))
    if args.dry_run or not notes:
        out(args, {"inbox": [f.name for f in notes]}, f"{len(notes)} inbox notes" + ("; nothing to fold" if not notes else ""))
        return 0
    with Lock():
        results = merge_rounds()
        left = len(list(INBOX.glob("*.md")))
    folded = max(0, len(notes) - left)
    out(args, {"runs": results, "folded": folded, "left": left},
        f"folded {folded} notes in {len(results)} model runs; {left} left")
    if LIMIT.is_set():
        return 5
    return 1 if any(r["rc"] != 0 for r in results) else 0


def session_events(path: Path) -> list[tuple[str, str]]:
    """(role, text) for each user and assistant message of a Claude Code or omp session file. Tool calls
    and tool output are left out, except as `[tool: name]` marks."""
    events = []
    for line in path.open(errors="replace"):
        try:
            msg = json.loads(line).get("message")
        except (ValueError, AttributeError):
            continue
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        content = msg.get("content")
        parts = [content] if isinstance(content, str) else [
            c.get("text") if c.get("type") == "text" else f"[tool: {c.get('name')}]" if c.get("type") in ("tool_use", "toolCall") else ""
            for c in content or [] if isinstance(c, dict)]
        text = "\n".join(x for x in parts if x).strip()
        if text:
            events.append((msg["role"], text))
    return events


def cmd_transcript(args) -> int:
    """A session's dialogue in bounded form, for the dream pass: numbered events, each cut at
    --max-chars, stopping at --budget-chars with the event number to continue from."""
    f = Path(os.path.expanduser(args.path))
    if not f.is_file():
        raise Usage(f"no session file at {f}")
    events, lines, used, nxt = session_events(f), [], 0, None
    for n, (role, text) in enumerate(events):
        if n < args.from_event:
            continue
        text = SECRET.sub("[REDACTED-SECRET]", text)
        if len(text) > args.max_chars:
            text = text[:args.max_chars] + f" … (+{len(text) - args.max_chars} chars)"
        line = f"[{n}] {role}: {text}"
        room = args.budget_chars - used
        if lines and len(line) > room:
            nxt = n
            break
        lines.append(line[:max(room, 0)])  # a first event larger than the whole budget is cut to it
        used += len(lines[-1]) + 2
    if nxt is not None:
        lines.append(f"(continue with --from-event {nxt}; {len(events)} events in all)")
    out(args, {"events": len(events), "next": nxt, "text": "\n\n".join(lines)}, "\n\n".join(lines) or "(no dialogue)")
    return 0


def cmd_verify(args) -> int:
    """How much of the memory files' exact detail the pages hold: every `code span` (a command, a path,
    a flag) and every date in a memory file is looked up verbatim in the open pages. No model."""
    ensure_repo()
    projects = source_projects()
    if args.project:
        projects = [find_project(projects, resolve_cwd(args.project))]
    squash = lambda t: re.sub(r"\s+", " ", t)
    pages = squash("\n".join(p.text for p in load_pages().values() if not p.gated))
    rows, total, found = [], 0, 0
    for proj in projects:
        for f in proj["files"]:
            try:
                _, body, _ = parse_frontmatter((PROJECTS / f["rel"]).read_text(errors="replace"))
            except OSError:
                continue
            spans = {squash(x) for x in re.findall(r"`([^`\n]{6,80})`", body)} | set(re.findall(r"\b20\d\d-\d\d-\d\d\b", body))
            if len(spans) < 4:
                continue
            hit = sum(1 for x in spans if x in pages)
            total, found = total + len(spans), found + hit
            rows.append({"file": f["rel"], "details": len(spans), "found": hit, "pct": round(100 * hit / len(spans))})
    rows.sort(key=lambda r: (r["pct"], r["file"]))
    pct = round(100 * found / total, 1) if total else None
    data = {"files": len(rows), "details": total, "found": found, "pct": pct, "lowest": rows[:args.n]}
    text = ["no memory file has enough exact detail to check" if pct is None else
            f"{found} of {total} exact details ({pct}%) from {len(rows)} memory files appear verbatim in pages"]
    text += [f"  {r['pct']:>3}%  {r['found']:>3}/{r['details']:<3}  {r['file']}" for r in rows[:args.n] if r["pct"] < 100]
    out(args, data, "\n".join(text))
    return 0


HOOK_MARK = "context --cwd"
LAUNCHD_LABEL, SYSTEMD_UNIT = "dev.wiki-memory.nightly", "wiki-memory-nightly"


def hook_command() -> str:
    return f'{shlex.quote(sys.executable)} {shlex.quote(str(CODE / "wiki.py"))} {HOOK_MARK} "$CLAUDE_PROJECT_DIR" --instructions'


def is_wiki_hook(h: dict) -> bool:
    return "wiki.py" in str(h.get("command")) and HOOK_MARK in str(h.get("command"))


def claude_hooks(settings: dict) -> list[dict]:
    """The SessionStart hook commands in Claude Code settings that run this CLI."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    entries = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    return [h for e in entries or [] if isinstance(e, dict) for h in e.get("hooks") or []
            if isinstance(h, dict) and is_wiki_hook(h)]


def install_claude(remove: bool) -> str:
    f = HOME / ".claude/settings.json"
    try:
        settings = json.loads(f.read_text()) if f.exists() else {}
    except ValueError as e:
        raise Usage(f"{f} is not valid JSON ({e}); fix it first, nothing was changed")
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict) \
            or not isinstance(settings.get("hooks", {}).get("SessionStart", []), list):
        raise Usage(f"{f} has an unexpected shape (hooks.SessionStart is not a list); nothing was changed")
    mine = claude_hooks(settings)
    if remove:
        if not mine:
            return "Claude Code: no wiki hook installed"
        kept = []
        for e in settings["hooks"]["SessionStart"]:  # only this CLI's command goes; its neighbours stay
            if isinstance(e, dict) and isinstance(e.get("hooks"), list):
                rest = [h for h in e["hooks"] if not (isinstance(h, dict) and is_wiki_hook(h))]
                if not rest:
                    continue
                e = {**e, "hooks": rest}
            kept.append(e)
        settings["hooks"]["SessionStart"] = kept
    elif mine:
        return f"Claude Code: hook already in {f}"
    else:
        settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(
            {"hooks": [{"type": "command", "command": hook_command()}]})
    mode = f.stat().st_mode & 0o777 if f.exists() else 0o600
    if f.exists():
        shutil.copy2(f, f.with_name(f"settings.json.bak-wiki-memory-{datetime.now():%Y%m%d-%H%M%S}"))
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".settings.json.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(settings, indent=2) + "\n")
    os.chmod(tmp, mode)
    tmp.replace(f)
    return f"Claude Code: SessionStart hook {'removed from' if remove else 'added to'} {f} (new sessions pick it up)"


def install_omp(remove: bool) -> str:
    f = HOME / ".omp/agent/extensions/wiki-memory.ts"
    if remove:
        existed = f.exists()
        f.unlink(missing_ok=True)
        return f"omp: {'removed ' + str(f) if existed else 'no extension installed'}"
    f.parent.mkdir(parents=True, exist_ok=True)
    src = (CODE / "adapters/omp/wiki-memory.ts").read_text()
    f.write_text(src.replace("__WIKI_COMMAND__", json.dumps([sys.executable, str(CODE / "wiki.py")])[1:-1]))
    return f"omp: extension written to {f} (new sessions load it)"


def install_bin(remove: bool) -> str:
    link = HOME / ".local/bin/wiki"
    if remove:
        if link.is_symlink() and os.path.realpath(link) == str(CODE / "wiki.py"):
            link.unlink()
            return f"removed {link}"
        return f"{link} is not this CLI's link; left alone"
    link.parent.mkdir(parents=True, exist_ok=True)
    if (link.exists() or link.is_symlink()) and os.path.realpath(link) != os.path.realpath(CODE / "wiki.py"):
        raise Usage(f"{link} already exists and is another program; move it away first")
    link.unlink(missing_ok=True)
    link.symlink_to(CODE / "wiki.py")
    os.chmod(CODE / "wiki.py", 0o755)
    on_path = str(link.parent) in os.environ.get("PATH", "").split(os.pathsep)
    return f"linked {link}" + ("" if on_path else f" (add {link.parent} to your PATH)")


def schedule_files() -> list[Path]:
    if sys.platform == "darwin":
        return [HOME / f"Library/LaunchAgents/{LAUNCHD_LABEL}.plist"]
    return [HOME / f".config/systemd/user/{SYSTEMD_UNIT}.{ext}" for ext in ("service", "timer")]


def schedule_active() -> bool:
    """Is the nightly job registered with the scheduler (not only written to disk)?"""
    if not all(f.exists() for f in schedule_files()):
        return False
    cmd = ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"] if sys.platform == "darwin" \
        else ["systemctl", "--user", "is-enabled", f"{SYSTEMD_UNIT}.timer"]
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
    except OSError:
        return False


def install_schedule(remove: bool, at: str) -> str:
    """A nightly `wiki nightly`: a LaunchAgent on macOS, a systemd user timer on Linux. The job gets
    today's PATH and WIKI_* settings, so it runs against the same wiki with the same runner."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at)
    if not m:
        raise Usage("--at takes HH:MM, 24-hour local time")
    hour, minute = int(m.group(1)), int(m.group(2))
    files, log = schedule_files(), STATE / "logs/nightly.log"
    argv = [sys.executable, str(CODE / "wiki.py"), "nightly"]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),  # the runner and git must be findable at night
           **{k: v for k, v in sorted(os.environ.items()) if k.startswith("WIKI_")}}
    if not remove and any(re.search(r"[\x00-\x1f\x7f]", x) for x in [*argv, *env, *env.values(), str(log)]):
        raise Usage("a path, PATH or a WIKI_* setting holds a control character; the job file would be unsafe")
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], capture_output=True)
        if remove:
            existed = files[0].exists()
            files[0].unlink(missing_ok=True)
            return "schedule: removed" if existed else "schedule: none installed"
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        log.parent.mkdir(parents=True, exist_ok=True)
        files[0].parent.mkdir(parents=True, exist_ok=True)
        files[0].write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n'
            f"  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n"
            "  <key>ProgramArguments</key><array>" + "".join(f"<string>{esc(a)}</string>" for a in argv) + "</array>\n"
            "  <key>EnvironmentVariables</key><dict>"
            + "".join(f"<key>{esc(k)}</key><string>{esc(v)}</string>" for k, v in env.items()) + "</dict>\n"
            f"  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer>"
            f"<key>Minute</key><integer>{minute}</integer></dict>\n"
            f"  <key>StandardOutPath</key><string>{esc(str(log))}</string>\n"
            f"  <key>StandardErrorPath</key><string>{esc(str(log))}</string>\n</dict></plist>\n")
        r = subprocess.run(["launchctl", "bootstrap", domain, str(files[0])], capture_output=True, text=True)
        if r.returncode != 0:
            files[0].unlink(missing_ok=True)  # nothing half-installed
            raise RuntimeError(f"launchctl bootstrap failed, no job installed: {r.stderr.strip()}")
        return f"schedule: `wiki nightly` every day at {hour:02d}:{minute:02d} (LaunchAgent {LAUNCHD_LABEL}; log {log})"
    ctl = ["systemctl", "--user"]

    def clear() -> bool:
        subprocess.run([*ctl, "disable", "--now", f"{SYSTEMD_UNIT}.timer"], capture_output=True)
        existed = any(f.exists() for f in files)
        for f in files:
            f.unlink(missing_ok=True)
        subprocess.run([*ctl, "daemon-reload"], capture_output=True)
        return existed

    if remove:
        return "schedule: removed" if clear() else "schedule: none installed"
    # systemd reads %, $ and quotes in unit files: escape them so a value stays one literal word
    unit = lambda t: '"' + t.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    files[0].parent.mkdir(parents=True, exist_ok=True)
    files[0].write_text("[Unit]\nDescription=wiki-memory nightly pass\n\n[Service]\nType=oneshot\n"
                        + "".join(f"Environment={unit(k + '=' + v)}\n" for k, v in env.items())
                        + "ExecStart=" + " ".join(unit(a).replace("$", "$$") for a in argv) + "\n")
    files[1].write_text("[Unit]\nDescription=wiki-memory nightly pass\n\n[Timer]\n"
                        f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    for cmd in (["daemon-reload"], ["enable", "--now", f"{SYSTEMD_UNIT}.timer"]):
        r = subprocess.run([*ctl, *cmd], capture_output=True, text=True)
        if r.returncode != 0:
            clear()  # nothing half-installed
            raise RuntimeError(f"systemctl --user {' '.join(cmd)} failed, no job installed: {r.stderr.strip()}")
    return (f"schedule: `wiki nightly` every day at {hour:02d}:{minute:02d} (systemd user timer {SYSTEMD_UNIT}; "
            f"`journalctl --user -u {SYSTEMD_UNIT}` for output)")


def cmd_install(args) -> int:
    """Wire the CLI into this machine: the `wiki` command, an agent's session-start hook, the nightly job."""
    todo = {"bin": lambda: install_bin(args.remove), "claude-code": lambda: install_claude(args.remove),
            "omp": lambda: install_omp(args.remove), "schedule": lambda: install_schedule(args.remove, args.at)}
    lines = [todo[t]() for t in args.targets]
    out(args, {"done": lines}, "\n".join(lines))
    return 0


def cmd_nightly(args) -> int:
    """The unattended pass: sync, re-import changed memory files, fold notes, sync. One line per step;
    a step that is busy, limited or failing never stops the next."""
    ensure_repo()
    remote = bool(git("remote", check=False).stdout.split())
    steps = [["sync"]] if remote else []
    if os.environ.get("WIKI_NOTES_ONLY") != "1":
        steps += [["import", "--refresh-all"], ["fold"]] + ([["sync"]] if remote else [])
    worst, done = 0, []
    for step in steps:
        r = subprocess.run([sys.executable, str(CODE / "wiki.py"), *step], capture_output=True, text=True,
                           errors="replace", stdin=subprocess.DEVNULL)
        last = next((line for line in reversed((r.stdout + r.stderr).splitlines()) if line.strip()), "")
        what = {0: "ok", 4: "skipped, the lock is held", 5: "stopped on a provider limit"}.get(r.returncode,
                                                                                             f"failed (exit {r.returncode})")
        done.append({"step": " ".join(step), "rc": r.returncode, "last": last[:300]})
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] wiki {' '.join(step)}: {what}; {last[:300]}", file=sys.stderr, flush=True)
        rank = {0: 0, 4: 1, 5: 2}.get(r.returncode, 3)  # failed outranks limited outranks busy
        if rank > {0: 0, 4: 1, 5: 2, 1: 3}[worst]:
            worst = r.returncode if r.returncode in (4, 5) else 1
    out(args, {"steps": done}, f"nightly: {len(done)} steps, {'all ok' if not worst else 'see the lines above'}")
    return worst


def cmd_doctor(args) -> int:
    """Is everything wired? One line per check; exit 1 when a required one fails."""
    checks: list[dict] = []

    def check(name: str, ok: Optional[bool], detail: str, fix: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    have = (WIKI / ".git").exists()
    check("wiki", have, str(WIKI) + (f": {len(load_pages())} pages" if have else " missing"), "wiki init")
    exe = shutil.which({"custom": RUNNER_CMD[0] if RUNNER_CMD else "", "omp": shlex.split(os.environ.get("WIKI_OMP", "omp"))[0]}
                       .get(RUNNER, RUNNER)) if RUNNER else None
    notes_only = os.environ.get("WIKI_NOTES_ONLY") == "1"
    check("model runner", True if notes_only else bool(exe),
          "not needed (WIKI_NOTES_ONLY=1)" if notes_only else (f"{RUN_LABEL} ({exe})" if exe else "none found"),
          "install Claude Code, Codex or omp, or set WIKI_RUNNER / WIKI_RUNNER_CMD")
    box = shutil.which("sandbox-exec" if sys.platform == "darwin" else "bwrap")
    off = os.environ.get("WIKI_SANDBOX", "1") == "0"
    check("sandbox", True if notes_only else (None if off else bool(box)),
          "off (WIKI_SANDBOX=0): model runs are unprotected" if off else (box or "none found"),
          "install bubblewrap" if sys.platform != "darwin" else "")
    link = shutil.which("wiki")
    check("wiki on PATH", bool(link) or None, link or "not on PATH; hooks call the script by its full path",
          "wiki.py install bin")
    try:
        settings = json.loads((HOME / ".claude/settings.json").read_text())
    except (OSError, ValueError):
        settings = {}
    hooks = [n for n, on in (("Claude Code", bool(claude_hooks(settings))),
                             ("omp", (HOME / ".omp/agent/extensions/wiki-memory.ts").exists())) if on]
    check("session hook", bool(hooks) or None, ", ".join(hooks) or "no agent loads the wiki yet",
          "wiki install claude-code   (or: wiki install omp)")
    sched = schedule_active()
    check("nightly job", sched or None, "registered" if sched else "not scheduled; notes are folded only when you run `wiki fold`",
          "wiki install schedule")
    if have:
        notes = pending_notes()
        last = max((c for c in (last_commit("^import "), last_commit("^note on "), last_commit("^fold ")) if c),
                   key=lambda c: c["date"], default=None)
        age, hours = "", 0
        if notes:
            try:
                hours = int((time.time() - min(datetime.fromisoformat(n["created"]).timestamp() for n in notes)) // 3600)
                age = f", oldest {hours} h"
            except ValueError:
                pass
        stale = hours > 48
        check("pending notes", None if stale else True, f"{len(notes)}{age}; last model pass or page note: "
              + (f"{last['date'][:16]}" if last else "never"), "wiki fold")
        if git("remote", check=False).stdout.split():
            ahead = git("rev-list", "--count", "@{u}..HEAD", check=False)
            check("sync", ahead.returncode == 0 or None,
                  f"{ahead.stdout.strip()} local commits not pushed" if ahead.returncode == 0 else "no upstream branch yet",
                  "wiki sync")
        else:
            check("sync", True, "no remote: this wiki lives on one machine")
        try:
            with Lock():
                check("lock", True, "free")
        except Locked:
            check("lock", None, "held: a model pass or sync is running")
    mark = {True: "ok  ", False: "FAIL", None: "note"}
    text = [f"{mark[c['ok']]}  {c['check']:<14} {c['detail']}" + (f"\n      fix: {c['fix']}" if c["ok"] is not True and c["fix"] else "")
            for c in checks]
    out(args, {"checks": checks}, "\n".join(text))
    return 1 if any(c["ok"] is False for c in checks) else 0


def cmd_dream(args) -> int:
    if not args.dry_run:
        model_host_check()
    since = time.time() - args.since_hours * 3600
    day = today()
    reports = STATE / "reports"
    report, report_json = reports / f"dream-{day}.md", reports / f"dream-{day}.json"
    if report.exists():
        stamp = datetime.now().strftime("%H%M")
        report, report_json = reports / f"dream-{day}-{stamp}.md", reports / f"dream-{day}-{stamp}.json"
    manifest, sessions = dream_manifest(since)
    work_name = f"dream-{datetime.now():%Y%m%d-%H%M}"
    work = STATE / "work" / work_name
    values = dict(DATE=day, WORK=str(work), REPORT=str(report), REPORT_JSON=str(report_json), TRANSCRIPT=f"{SELF} transcript",
                  SCHEMA=schema_text(), MANIFEST=manifest, CHECK=f"{SELF} _check {work}")
    if args.dry_run:
        prompt = render("dream.md", **values)
        out(args, {"date": day, "sessions": len(sessions), "report": str(report), "prompt": prompt}, prompt)
        return 0
    try:
        lock = Lock().__enter__()
    except Locked:
        out(args, {"error": "locked"}, "another import/dream/undo holds the lock")
        return 4
    try:
        init_repo()
        reports.mkdir(parents=True, exist_ok=True)
        snap = commit(f"snapshot before dream {day}")
        with INTEGRATE:
            work, base = prepare_work(work_name)
        rc, secs = run_model(render("dream.md", **values), work, STATE / "logs" / f"{work_name}.log", DREAM_MAX_TIME,
                             [report, report_json])
        res = integrate(work, base, work_name)
        ok = rc == 0 and report.exists()
        append_log("dream", day + ("" if ok else f" (exit {rc}{', no report' if not report.exists() else ''})"),
                   [f"{len(sessions)} sessions in manifest since {datetime.fromtimestamp(since):%Y-%m-%d %H:%M}",
                    f"runner {RUN_LABEL}, {secs / 60:.1f} min, report {report}", *result_lines(res)])
        new = commit(f"dream {day}")
        record_run({"kind": "dream", "batch": work_name, "rc": rc, "seconds": round(secs), "commit": new,
                    "date": datetime.now().isoformat(timespec="seconds")})
        if rc == 0:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        lock.__exit__(None, None, None)
    data = {"date": day, "ok": ok, "ompExit": rc, "minutes": round(secs / 60, 1), "sessions": len(sessions),
            "snapshot": snap, "commit": new, "report": str(report) if report.exists() else None,
            "changes": {k: v for k, v in res.items() if v}}
    out(args, data, f"dream {day}: {'ok' if ok else 'FAILED'} ({secs / 60:.0f} min); +{len(res['added'])} pages, "
                    f"~{len(res['updated'])} updated, {len(res['conflicts'])} conflicts; report {data['report']}; "
                    f"undo: `wiki undo {new[:7]}`" if new else "no changes")
    return 0 if ok else 1


# ---- main --------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    p = argparse.ArgumentParser(prog="wiki", description=__doc__.split("\n\n")[0], parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def verb(name, fn, help_):
        s = sub.add_parser(name, parents=[common], help=help_)
        s.set_defaults(fn=fn)
        return s

    verb("init", cmd_init, "create the data repo")
    verb("status", cmd_status, "page counts, last import/dream, git state, lint")
    verb("index", cmd_index, "print the index").add_argument("--cwd")
    s = verb("context", cmd_context, "session-start context block")
    s.add_argument("--cwd")
    s.add_argument("--budget-chars", type=int, default=16000,
                   help="hard cap on output chars (default 16000); below the minimal header, the header is "
                        "cut with a note and the exit code is 0")
    s.add_argument("--instructions", action="store_true",
                   help="prepend the recall and `wiki note` instructions an agent session needs "
                        "(about 2,300 chars on top of --budget-chars)")
    verb("read", cmd_read, "print pages").add_argument("slugs", nargs="+")
    s = verb("search", cmd_search, "ranked literal search")
    s.add_argument("terms", nargs="+")
    s.add_argument("--cwd")
    s.add_argument("--gated", action="store_true")
    s.add_argument("-n", type=int, default=15)
    verb("backlinks", cmd_backlinks, "pages linking to a page").add_argument("slug")
    verb("log", cmd_log, "recent log entries").add_argument("-n", type=int, default=10)
    verb("lint", cmd_lint, "check the wiki").add_argument("--fix", action="store_true")
    s = verb("history", cmd_history, "commits touching a page")
    s.add_argument("slug")
    s.add_argument("-n", type=int, default=20)
    verb("diff", cmd_diff, "uncommitted changes or a commit").add_argument("ref", nargs="?")
    s = verb("undo", cmd_undo, "revert a commit as a new commit")
    s.add_argument("ref")
    s.add_argument("--dry-run", action="store_true")
    s = verb("import", cmd_import, "(model) compile Claude memory into pages")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true")
    g.add_argument("--project", metavar="CWD", help="that project's files only, as batches proj-<hub>-N")
    g.add_argument("--gaps", action="store_true", help="import memory files no page lists yet")
    g.add_argument("--refresh", nargs="+", metavar="PATH",
                   help="re-import these memory files (<slug>/memory/<file>.md or absolute) whose content changed; "
                        "batches refresh-<YYYYMMDD-HHMMSS>-<rand>-N tell the model they supersede the pages")
    g.add_argument("--refresh-project", metavar="CWD",
                   help="refresh that project's files no page lists or whose content differs from the ingestion "
                        "ledger (state/ingested.json)")
    g.add_argument("--refresh-all", action="store_true",
                   help="--refresh-project for every project not in exclude.txt; gated files are re-copied verbatim")
    g.add_argument("--mark-ingested", action="store_true",
                   help="no model: record the current content hash of every memory file pages list")
    g.add_argument("--report", action="store_true", help="coverage report, no model")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--parallel", type=int, default=4)
    s.add_argument("--replan", action="store_true", help="rebuild the stored batch plan")
    s = verb("verify", cmd_verify, "how much exact detail from the memory files the pages hold (no model)")
    s.add_argument("--project", metavar="CWD")
    s.add_argument("-n", type=int, default=10, help="how many of the weakest files to list")
    s = verb("transcript", cmd_transcript, "a session file's dialogue, bounded (used by dream)")
    s.add_argument("path")
    s.add_argument("--max-chars", type=int, default=1500)
    s.add_argument("--budget-chars", type=int, default=12000)
    s.add_argument("--from-event", type=int, default=0)
    verb("doctor", cmd_doctor, "check the install: runner, sandbox, hooks, schedule, sync")
    s = verb("install", cmd_install, "wire the CLI into this machine")
    s.add_argument("targets", nargs="+", choices=("bin", "claude-code", "omp", "schedule"))
    s.add_argument("--remove", action="store_true")
    s.add_argument("--at", default="04:30", help="schedule: HH:MM local time (default 04:30)")
    verb("nightly", cmd_nightly, "(model) the unattended pass: sync, refresh, fold, sync")
    s = verb("note", cmd_note, "save a fact for later folding (no model)")
    s.add_argument("--title", required=True)
    s.add_argument("--type", required=True, help="user | feedback | project | reference")
    s.add_argument("--cwd", help="project the fact belongs to (default: current directory)")
    s.add_argument("--body", help="note text (default: stdin)")
    s.add_argument("--gated", metavar="TRIGGER", help="keep out of context; read only when TRIGGER applies")
    s.add_argument("--page", metavar="SLUG", help="append the fact to this page now, verbatim, without a model")
    s = verb("notes", cmd_notes, "list pending agent notes")
    s.add_argument("--cwd")
    s.add_argument("--all", action="store_true")
    verb("sync", cmd_sync, "commit notes, pull and push the shared wiki")
    verb("fold", cmd_fold, "(model) fold pending notes into pages").add_argument("--dry-run", action="store_true")
    s = verb("dream", cmd_dream, "(model) ingest recent sessions")
    s.add_argument("--since-hours", type=float, default=24)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("_redact-since", parents=[common])
    s.add_argument("since")
    s.set_defaults(fn=cmd_redact_since)
    s = sub.add_parser("_check", parents=[common])
    s.add_argument("work")
    s.set_defaults(fn=cmd_check)
    return p


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        return args.fn(args)
    except Usage as e:
        out(args, {"error": str(e)}, f"wiki: {e}")
        return 2
    except Locked:
        out(args, {"error": "locked"}, "another import/dream/undo holds the lock")
        return 4
    except RuntimeError as e:
        out(args, {"error": str(e)}, f"wiki: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
