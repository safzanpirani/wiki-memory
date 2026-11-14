#!/usr/bin/env python3
"""Smoke tests: a temp HOME, WIKI_HOME, WIKI_STATE and fake memory dir, and tests/fake_omp.py in place
of the model. Run: python3 -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
WIKI_PY = CODE / "wiki.py"


def slug(p: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", p)


class Base(unittest.TestCase):
    """A fresh temp tree per test class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wiki-test-")).resolve()
        home = cls.tmp / "home"
        cls.home = home
        cls.projects = home / ".claude/projects"
        cls.env = dict(os.environ, HOME=str(home), WIKI_HOME=str(cls.tmp / "wiki"), WIKI_STATE=str(cls.tmp / "state"),
                       WIKI_PROJECTS=str(cls.projects), WIKI_OMP_SESSIONS=str(home / ".omp/agent/sessions"),
                       WIKI_EXCLUDE=str(cls.tmp / "exclude.txt"), WIKI_BATCH_CHARS="400",
                       WIKI_OMP=f"{sys.executable} {CODE / 'tests/fake_omp.py'}",
                       GIT_CONFIG_GLOBAL=str(Path.home() / ".gitconfig"))
        cls.dotfiles = str(home / "Development/dotfiles")
        cls.env.update(WIKI_GLOBAL_DIRS=cls.dotfiles, WIKI_RUNNER="omp", WIKI_CONFIG=str(cls.tmp / "config"),
                       WIKI_UMBRELLA=os.pathsep.join([str(home / "Development"), str(home / "Development/projects")]))
        cls.alpha = str(home / "Development/projects/alpha")
        cls.beta = str(home / "Development/beta")
        secret_dir = str(home / "Development/projects/secret")
        (cls.tmp / "exclude.txt").write_text(f"# test\n{secret_dir}\n")
        for cwd, files in [(cls.dotfiles, {"host-main.md": 150, "user-role.md": 150, "feedback-git.md": 150}),
                           (cls.alpha, {"alpha-deploy.md": 200, "alpha-db.md": 200, "alpha-ui.md": 200}),
                           (cls.beta, {"beta-notes.md": 100}),
                           (secret_dir, {"secret-plan.md": 100})]:
            Path(cwd).mkdir(parents=True, exist_ok=True)
            d = cls.projects / slug(cwd) / "memory"
            d.mkdir(parents=True)
            (d.parent / "s.jsonl").write_text(json.dumps({"type": "session", "cwd": cwd}) + "\n")
            for name, size in files.items():
                (d / name).write_text(f"---\nname: {name[:-3]}\ndescription: about {name}\nmetadata:\n  type: project\n"
                                      f"---\n{'fact ' * (size // 5)}\n")
            (d / "MEMORY.md").write_text("# Memory\n")
        g = cls.projects / slug(cls.alpha) / "memory" / "DO NOT READ UNLESS ZEBRA IS MENTIONED.md"
        g.write_text("---\nname: zebra\ndescription: gated\n---\nZEBRA BODY VERBATIM\n")
        cls.gated_src = g.read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wiki(self, *args, ok=(0,)):
        r = subprocess.run([sys.executable, str(WIKI_PY), *args], env=self.env, capture_output=True, text=True)
        if r.returncode not in ok:
            self.fail(f"wiki {' '.join(args)} exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def j(self, *args, ok=(0,)):
        return json.loads(self.wiki(*args, "--json", ok=ok).stdout)

    def py(self, code: str, **env) -> object:
        """Run code against the wiki module in this fixture's env; returns the JSON it prints last."""
        r = subprocess.run([sys.executable, "-c", f"import sys, json; sys.path.insert(0, {str(CODE)!r})\n"
                            "import wiki\n" + code], env={**self.env, **env}, capture_output=True, text=True)
        if r.returncode:
            self.fail(f"python exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        return json.loads(r.stdout.strip().splitlines()[-1])

    def memory(self, cwd: str, name: str, text: str, session: bool = True) -> Path:
        d = self.projects / slug(cwd) / "memory"
        d.mkdir(parents=True, exist_ok=True)
        if session:
            (d.parent / "s.jsonl").write_text(json.dumps({"type": "session", "cwd": cwd}) + "\n")
        (d / name).write_text(text)
        return d / name


class WikiSmoke(Base):

    def test_all(self):
        # plan: excluded project absent, global tier first
        plan = self.j("import", "--all", "--dry-run")["plan"]
        names = [b["name"] for b in plan]
        self.assertTrue(names[0].startswith("g01-dotfiles"), names)
        self.assertFalse(any("secret" in f for b in plan for f in b["files"]))
        self.assertGreaterEqual(len(plan), 4)

        res = self.j("import", "--all", "--parallel", "3")
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["coverage"], 100.0)
        wiki = Path(self.env["WIKI_HOME"])
        # gated copy is verbatim and appears only under ## Gated
        gp = wiki / "pages/gated-zebra-is-mentioned.md"
        self.assertTrue(gp.read_text().endswith(self.gated_src))
        index = (wiki / "index.md").read_text()
        self.assertIn("## Gated\n\n- [[gated-zebra-is-mentioned]] — DO NOT READ UNLESS ZEBRA IS MENTIONED", index)
        self.assertNotIn("ZEBRA BODY", index)
        # parallel collisions went through the inbox and were merged; nothing left behind
        self.assertEqual(list((wiki / "inbox").glob("*.md")), [])
        self.assertIn("- merged shared-tool--", (wiki / "pages/shared-tool.md").read_text())
        subjects = subprocess.run(["git", "-C", str(wiki), "log", "--format=%s"], capture_output=True,
                                  text=True).stdout.splitlines()
        for n in names:
            self.assertIn(f"import {n}", subjects)
        self.assertTrue(any(s.startswith("import merge-") for s in subjects))

        # resumable: a second run starts no batch
        again = self.j("import", "--all")
        self.assertFalse(any(r["batch"] in names for r in again["results"]))

        cov = self.j("import", "--report")
        self.assertEqual(cov["missing"], [])
        self.assertEqual(cov["gatedCovered"], 1)

        # status / index / search / backlinks / read
        st = self.j("status")
        self.assertGreater(st["byKind"]["project"], 0)
        self.assertEqual(st["byKind"]["gated"], 1)
        scoped = self.j("index", "--cwd", self.alpha)
        lines = "\n".join(line for s in scoped["sections"] for line in s["lines"])
        self.assertIn("[[alpha]]", lines)
        self.assertNotIn("[[beta]]", lines)
        self.assertIn("[[shared-tool]]", lines)
        self.assertEqual(scoped["hubs"][0], "alpha")
        hits = self.j("search", "alpha", "deploy")["hits"]
        self.assertEqual(hits[0]["slug"], "alpha-deploy")
        self.assertFalse(any(h["slug"].startswith("gated-") for h in self.j("search", "zebra", ok=(0, 1))["hits"]))
        self.assertTrue(any(h["slug"].startswith("gated-") for h in self.j("search", "zebra", "--gated")["hits"]))
        self.assertIn("alpha", self.j("backlinks", "alpha-db")["backlinks"])
        self.assertEqual(self.j("read", "alpha")[0]["slug"], "alpha")

        # context: hub + core page bodies, inside budget, no gated body
        ctx = self.j("context", "--cwd", self.alpha, "--budget-chars", "3000")
        self.assertLessEqual(ctx["chars"], 3000)
        self.assertIn("alpha", ctx["pagesIncluded"])
        self.assertIn("user-profile", ctx["pagesIncluded"])
        self.assertNotIn("ZEBRA BODY", ctx["text"])
        for budget in ("900", "300"):
            small = self.j("context", "--cwd", self.alpha, "--budget-chars", budget)
            self.assertLessEqual(len(small["text"]), int(budget))
        full = self.j("context", "--cwd", self.alpha, "--instructions")
        self.assertEqual(full["chars"], len(full["text"]))
        self.assertIn("wiki note --type", full["text"])
        tiny = self.j("context", "--cwd", self.alpha, "--budget-chars", "60")
        self.assertLessEqual(tiny["chars"], 60)
        self.assertTrue(tiny["headerCut"])
        self.wiki("context", "--cwd", self.alpha, "--budget-chars", "0", ok=(2,))

        # lint: break a page, --fix normalizes it and the index
        lint = self.j("lint", ok=(0, 3))
        self.assertFalse(lint["indexDrift"])
        bad = wiki / "pages/beta.md"
        bad.write_text(bad.read_text().replace("tags: [test]\n", "").replace("updated: 2026-11-13\n", "")
                       + "\n[[nowhere]]\n")
        lint = self.j("lint", ok=(3,))
        self.assertIn({"page": "beta", "link": "nowhere"}, lint["dangling"])
        self.assertTrue(any(e["page"] == "beta" and e["error"] == "missing updated" for e in lint["frontmatter"]))
        fixed = self.j("lint", "--fix", ok=(0, 3))
        self.assertIn("beta", fixed["fixed"]["pages"])
        self.assertFalse(any(e["page"] == "beta" for e in fixed["frontmatter"]))

        # undo: revert the dream's commit, and a conflict once a page changed afterwards
        dream = self.j("dream", "--since-hours", "1")
        self.assertTrue(dream["ok"], dream)
        self.assertTrue((wiki / "pages/dream-note.md").exists())
        dry = self.j("undo", dream["commit"], "--dry-run")
        self.assertEqual([f["action"] for f in dry["files"]], ["delete"])
        self.assertTrue((wiki / "pages/dream-note.md").exists())
        self.j("undo", dream["commit"])
        self.assertFalse((wiki / "pages/dream-note.md").exists())
        self.assertIn("undo", self.wiki("log", "-n", "1").stdout)
        alpha_commit = subprocess.run(["git", "-C", str(wiki), "log", "-1", "--format=%H", "--", "pages/alpha.md"],
                                      capture_output=True, text=True).stdout.strip()
        (wiki / "pages/alpha.md").write_text((wiki / "pages/alpha.md").read_text() + "\nlocal edit\n")
        conf = self.j("undo", alpha_commit, "--dry-run", ok=(3,))
        self.assertIn("pages/alpha.md", [c["path"] for c in conf["conflicts"]])
        self.assertTrue(self.j("history", "alpha")["commits"])
        self.assertIn("local edit", self.j("diff")["patch"])


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


class Completion(Base):
    def test_coverage_decides_done(self):
        wiki = Path(self.env["WIKI_HOME"])
        # a run that exits 0 and writes nothing commits, but leaves its batches to redo
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--all", "--json"],
                           env=dict(self.env, FAKE_OMP_NOOP="1"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(json.loads(r.stdout)["coverage"], 0.0)
        states = self.j("import", "--all", "--dry-run")["states"]
        self.assertTrue(states and all(s["state"] == "redo" for s in states.values()), states)

        # the next run imports them; after it every batch is done and a further run starts none
        self.assertEqual(self.j("import", "--all")["coverage"], 100.0)
        dry = self.j("import", "--all", "--dry-run")
        self.assertTrue(all(s["state"] == "done" for s in dry["states"].values()), dry["states"])
        names = [b["name"] for b in dry["plan"]]
        self.assertFalse([r["batch"] for r in self.j("import", "--all")["results"] if r["batch"] in names])

        # pages gone (an undo, a hand edit): the batch runs again for the files no page lists any more
        holder = next(b["name"] for b in dry["plan"] if any(f.endswith("/alpha-db.md") for f in b["files"]))
        (wiki / "pages/alpha.md").unlink()
        (wiki / "pages/alpha-db.md").unlink()
        git(wiki, "commit", "-qam", "drop pages")
        st = self.j("import", "--all", "--dry-run")["states"][holder]
        self.assertEqual((st["state"], [f.rsplit("/", 1)[1] for f in st["files"]]), ("redo", ["alpha-db.md"]))
        res = self.j("import", "--all")
        self.assertEqual([r["batch"] for r in res["results"] if r["batch"] in names], [holder])
        self.assertTrue((wiki / "pages/alpha-db.md").exists())

        # a file gated after the plan was stored is dropped from it and copied verbatim instead
        note = self.projects / slug(self.beta) / "memory/beta-notes.md"
        note.write_text("---\nname: beta\ndescription: >-\n  private. DO NOT READ\n  UNLESS the user says BETA\n---\n"
                        "BETA PRIVATE BODY\n")
        rel = f"{slug(self.beta)}/memory/beta-notes.md"
        self.assertIn(rel, self.j("import", "--all", "--dry-run")["dropped"])
        self.j("import", "--all")
        self.assertIn("DO NOT READ UNLESS the user says BETA", (wiki / "index.md").read_text())
        gated = [p for p in (wiki / "pages").glob("gated-*.md") if rel in p.read_text()]
        self.assertEqual(len(gated), 1)
        self.assertTrue(gated[0].read_text().endswith("BETA PRIVATE BODY\n"))


class ProviderLimit(Base):
    def test_limit_stops_new_batches(self):
        calls = self.tmp / "calls"
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--all", "--parallel", "1"],
                           env=dict(self.env, FAKE_OMP_LIMIT="1", FAKE_OMP_CALLS=str(calls)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"provider limit: retry after \d\d:\d\d")
        self.assertEqual(len(calls.read_text().splitlines()), 1)  # no batch launched after the 429
        dry = self.j("import", "--all", "--dry-run")
        subjects = git(Path(self.env["WIKI_HOME"]), "log", "--format=%s").splitlines()
        batch_commits = [s for s in subjects for b in dry["plan"] if s.startswith(f"import {b['name']}")]
        self.assertEqual(batch_commits, [])  # the limited run changed nothing, so it left no empty partial
        self.assertTrue(all(s["state"] == "todo" for s in dry["states"].values()), dry["states"])


class Refresh(Base):
    def test_refresh_changed_sources(self):
        wiki = Path(self.env["WIKI_HOME"])
        self.assertEqual(self.j("import", "--all")["coverage"], 100.0)
        rel = f"{slug(self.alpha)}/memory/alpha-db.md"
        self.assertEqual(self.j("import", "--refresh-project", self.alpha, "--dry-run")["plan"], [])
        self.wiki("import", "--refresh", "nowhere/memory/x.md", ok=(2,))

        src = self.projects / rel
        src.write_text(src.read_text() + "NEW FACT: the db moved to port 6543\n")
        dry = self.j("import", "--refresh-project", self.alpha, "--dry-run")
        self.assertEqual([b["files"] for b in dry["plan"]], [[rel]])  # only the changed file
        self.assertRegex(dry["plan"][0]["name"], r"^refresh-\d{8}-\d{6}-[0-9a-f]{4}-1$")
        self.assertEqual(self.j("import", "--refresh", str(src), "--dry-run")["plan"][0]["files"], [rel])

        prompts = self.tmp / "refresh-prompts"
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--refresh-project", self.alpha, "--json"],
                           env=dict(self.env, FAKE_OMP_PROMPT=str(prompts)), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        name = next(x["batch"] for x in json.loads(r.stdout)["results"] if x["batch"].startswith("refresh-"))
        self.assertIn(f"import {name}", git(wiki, "log", "--format=%s").splitlines())
        self.assertIn("supersedes what the pages say", prompts.read_text())
        self.assertIn("NEW FACT", (wiki / "pages/alpha-db.md").read_text())
        # the clean refresh is in the ledger: nothing is stale any more
        self.assertEqual(self.j("import", "--refresh-project", self.alpha, "--dry-run")["plan"], [])


class LedgerBase(Base):
    """Freshness by the ingestion ledger: each test imports everything first."""

    def setUp(self):
        self.wiki_dir = Path(self.env["WIKI_HOME"])
        self.assertEqual(self.j("import", "--all")["coverage"], 100.0)
        self.rel = f"{slug(self.alpha)}/memory/alpha-db.md"
        self.src = self.projects / self.rel

    def ledger(self) -> dict:
        f = Path(self.env["WIKI_STATE"]) / "ingested.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def stale(self) -> list[str]:
        plan = self.j("import", "--refresh-project", self.alpha, "--dry-run")["plan"]
        return [f for b in plan for f in b["files"]]

    def unlist(self, rel: str) -> None:
        """Drop rel from the `sources` of every page that lists it."""
        for p in (self.wiki_dir / "pages").glob("*.md"):
            text = p.read_text()
            for line in (f"  - {rel}\n", f"  - {json.dumps(rel)}\n"):  # pages quote a leading `-`
                text = text.replace(line, "")
            p.write_text(text)

    def sha256(self, path: Path) -> str:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def compiles(self, calls: Path) -> int:
        return sum(1 for line in calls.read_text().splitlines() if line.startswith("You are compiling"))


class LedgerSameMinute(LedgerBase):
    def test_two_refreshes_in_one_minute_both_run(self):
        names = []
        for fact in ("FIRST FACT", "SECOND FACT"):
            self.src.write_text(self.src.read_text() + fact + "\n")
            res = self.j("import", "--refresh", str(self.src))
            ran = [r for r in res["results"] if r["batch"].startswith("refresh-")]
            self.assertEqual([(r["rc"], r["left"]) for r in ran], [(0, [])], res)
            self.assertRegex(ran[0]["batch"], r"^refresh-\d{8}-\d{6}-[0-9a-f]{4}-1$")
            names.append(ran[0]["batch"])
        self.assertNotEqual(names[0], names[1])
        self.assertIn("SECOND FACT", (self.wiki_dir / "pages/alpha-db.md").read_text())
        self.assertEqual(self.ledger()[self.rel]["sha256"], self.sha256(self.src))
        self.assertEqual(self.stale(), [])


class LedgerLastReference(LedgerBase):
    def test_losing_last_reference_is_stale(self):
        self.assertEqual(self.stale(), [])
        self.unlist(self.rel)
        git(self.wiki_dir, "commit", "-qam", "drop the last reference")
        self.assertEqual(self.ledger()[self.rel]["sha256"], self.sha256(self.src))  # content unchanged
        self.assertEqual(self.stale(), [self.rel])


class LedgerMovedReference(LedgerBase):
    def test_moved_reference_is_not_stale_and_mark_ingested(self):
        self.unlist(self.rel)
        beta = self.wiki_dir / "pages/beta.md"
        beta.write_text(beta.read_text().replace("sources:\n", f"sources:\n  - {json.dumps(self.rel)}\n", 1))
        git(self.wiki_dir, "commit", "-qam", "move the reference to beta")
        future = time.time() + 30  # touched, not changed: the old mtime-vs-commit check called this stale
        os.utime(self.src, (future, future))
        self.assertEqual(self.stale(), [])

        # without a ledger entry the mtime fallback decides; --mark-ingested records what pages list now
        (Path(self.env["WIKI_STATE"]) / "ingested.json").unlink()
        old = time.time() - 3600  # the other files must predate the import commit by more than a second
        for f in self.src.parent.glob("*.md"):
            if f != self.src:
                os.utime(f, (old, old))
        self.assertEqual(self.stale(), [self.rel])
        marked = self.j("import", "--mark-ingested")
        self.assertEqual(marked["marked"], 8)  # 7 memory files and the gated one
        self.assertEqual(self.ledger()[self.rel]["sha256"], self.sha256(self.src))
        self.assertEqual(self.stale(), [])


class LedgerEditDuringRun(LedgerBase):
    def test_edit_during_refresh_stays_stale(self):
        self.src.write_text(self.src.read_text() + "STAGED FACT\n")
        staged = self.sha256(self.src)
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--refresh", str(self.src), "--json"],
                           env=dict(self.env, FAKE_OMP_EDIT=str(self.src), WIKI_SANDBOX="0"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        page = (self.wiki_dir / "pages/alpha-db.md").read_text()
        self.assertIn("STAGED FACT", page)
        self.assertNotIn("EDITED MID-RUN", page)
        self.assertIn("EDITED MID-RUN", self.src.read_text())
        self.assertEqual(self.ledger()[self.rel]["sha256"], staged)  # what the run saw, not the file now
        self.assertEqual(self.stale(), [self.rel])


class LedgerFailedCheck(LedgerBase):
    def test_exit_zero_with_failed_check_is_retried_and_not_recorded(self):
        new = self.memory(self.alpha, "alpha-new.md", "---\nname: alpha-new\ndescription: d\n---\nnew fact\n")
        rel = f"{slug(self.alpha)}/memory/alpha-new.md"
        self.assertEqual(self.stale(), [rel])  # no page lists it

        calls = self.tmp / "noop-calls"
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--refresh-project", self.alpha, "--json"],
                           env=dict(self.env, FAKE_OMP_NOOP="1", FAKE_OMP_CALLS=str(calls)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.compiles(calls), 2)  # the run and one retry, both exit 0 with the file uncovered
        ran = [x for x in json.loads(r.stdout)["results"] if x["batch"].startswith("refresh-")]
        self.assertEqual([(x["rc"], x["left"], x["batch"].endswith("-retry")) for x in ran], [(0, [rel], True)])
        self.assertNotIn(rel, self.ledger())
        self.assertEqual(self.stale(), [rel])

        calls2 = self.tmp / "noop-once-calls"
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--refresh-project", self.alpha, "--json"],
                           env=dict(self.env, FAKE_OMP_NOOP_ONCE=str(self.tmp / "noop-once"),
                                    FAKE_OMP_CALLS=str(calls2)), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.compiles(calls2), 2)  # the failed check got its retry, which covered the file
        ran = [x for x in json.loads(r.stdout)["results"] if x["batch"].startswith("refresh-")]
        self.assertEqual([(x["left"], x["batch"].endswith("-retry")) for x in ran], [([], True)])
        self.assertEqual(self.ledger()[rel]["sha256"], self.sha256(new))
        self.assertEqual(self.stale(), [])


class RefreshAll(LedgerBase):
    def test_refresh_all_takes_every_projects_stale_files(self):
        def files(res: dict) -> list[str]:
            return sorted(f for b in res["plan"] for f in b["files"])

        self.assertEqual(files(self.j("import", "--refresh-all", "--dry-run")), [])
        beta_rel = f"{slug(self.beta)}/memory/beta-notes.md"
        secret = self.projects / slug(str(self.home / "Development/projects/secret")) / "memory/secret-plan.md"
        gated = self.projects / slug(self.alpha) / "memory/DO NOT READ UNLESS ZEBRA IS MENTIONED.md"
        for f in (self.src, self.projects / beta_rel, secret, gated):
            f.write_text(f.read_text() + "REFRESHED FACT\n")

        dry = self.j("import", "--refresh-all", "--dry-run")
        self.assertEqual(files(dry), sorted([self.rel, beta_rel]))  # not the excluded or the gated file
        self.assertEqual(len(dry["plan"]), 2)  # one batch per project
        self.assertRegex(self.wiki("import", "--refresh-all", "--dry-run").stdout, r"^2 batches, \d+ KB, 2 files")

        dump = self.tmp / "refresh-all-dump"
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--refresh-all", "--json"],
                           env=dict(self.env, FAKE_OMP_DUMP=str(dump)), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        seen = dump.read_text()
        self.assertNotIn("secret-plan", seen)
        self.assertNotIn("ZEBRA BODY", seen)
        self.assertIn("REFRESHED FACT", (self.wiki_dir / "pages/alpha-db.md").read_text())
        self.assertIn("REFRESHED FACT", (self.wiki_dir / "pages/beta-notes.md").read_text())
        gated_pages = [p.read_text() for p in (self.wiki_dir / "pages").glob("gated-*.md")]
        self.assertTrue(any("ZEBRA BODY VERBATIM\nREFRESHED FACT" in t for t in gated_pages))  # re-copied, no model
        self.assertEqual(files(self.j("import", "--refresh-all", "--dry-run")), [])


class Staging(Base):
    def test_staged_sources_are_redacted(self):
        token = "ghp_" + "c" * 30
        src = self.projects / slug(self.beta) / "memory/beta-notes.md"
        src.write_text(src.read_text() + f"deploy key {token}\n")
        dump = self.tmp / "model-input"
        r = subprocess.run([sys.executable, str(WIKI_PY), "import", "--project", self.beta],
                           env=dict(self.env, FAKE_OMP_DUMP=str(dump)), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        seen = dump.read_text()
        self.assertIn("deploy key [REDACTED-SECRET]", seen)
        self.assertNotIn(token, seen)
        self.assertIn(token, src.read_text())  # the memory file itself is untouched


class Notes(Base):
    def test_note_context_fold(self):
        self.wiki("init")
        token = "sk-" + "a1B2" * 8
        n = self.j("note", "--title", f"Deploys go through make {token}", "--type", "project", "--cwd", self.alpha,
                   "--body", f"alpha deploys with make, key {token}")
        text = Path(n["note"]).read_text()
        self.assertNotIn(token, text)  # neither the title nor the body keeps a token
        self.assertIn("[REDACTED-SECRET]", text)
        self.assertTrue(Path(n["note"]).name.startswith("_agent--"))
        r = subprocess.run([sys.executable, str(WIKI_PY), "note", "--title", "Prefers short replies", "--type", "user"],
                           env=self.env, input="reply in under ten lines\n", capture_output=True, text=True,
                           cwd=self.beta)
        self.assertEqual(r.returncode, 0, r.stderr)
        # a trigger with a line break that would end the frontmatter key must still gate the note
        self.wiki("note", "--title", "Zebra plan", "--type", "project", "--cwd", self.alpha, "--gated",
                  "do not read unless ZEBRA IS\ngated: ", "--body", "ZEBRA NOTE BODY")
        self.wiki("note", "--title", "x", "--type", "nope", "--body", "y", ok=(2,))
        self.wiki("note", "--title", "x", "--type", "user", "--body", "  ", ok=(2,))
        self.wiki("note", "--title", "x", "--type", "user", "--body", "y", "--cwd", "/tmp/a\n---\nb", ok=(2,))
        # two triggers that start alike must not share a page
        long = "the yellow giraffe project is named in the task by the user"
        self.wiki("note", "--title", "G1", "--type", "project", "--gated", long + " once", "--body", "GIRAFFE ONE")
        self.wiki("note", "--title", "G2", "--type", "project", "--gated", long + " twice", "--body", "GIRAFFE TWO")

        alpha = self.j("context", "--cwd", self.alpha)
        self.assertIn("alpha deploys with make", alpha["text"])
        self.assertIn("reply in under ten lines", alpha["text"])  # user notes apply everywhere
        self.assertNotIn("ZEBRA NOTE BODY", alpha["text"])
        self.assertEqual(len(alpha["notesIncluded"]), 2)
        beta = self.j("context", "--cwd", self.beta)
        self.assertNotIn("alpha deploys with make", beta["text"])  # another project's note stays out
        self.assertIn("reply in under ten lines", beta["text"])
        small = self.j("context", "--cwd", self.alpha, "--budget-chars", "900")
        self.assertLessEqual(small["chars"], 900)
        self.assertEqual([x["title"] for x in self.j("notes", "--cwd", self.beta)], ["Prefers short replies"])
        listed = self.j("notes", "--all")
        self.assertEqual(len(listed), 5)
        zebra = next(x for x in listed if x["title"] == "Zebra plan")
        self.assertEqual((zebra["gated"], zebra["body"]), ("ZEBRA IS gated:", None))  # listed, body withheld
        self.assertNotIn("ZEBRA NOTE BODY", self.wiki("notes", "--all").stdout)

        r = subprocess.run([sys.executable, str(WIKI_PY), "fold"], env=dict(self.env, WIKI_NOTES_ONLY="1"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)  # a notes-only host never runs a model pass
        self.assertEqual(len(self.j("notes", "--all")), 5)

        res = self.j("fold")
        self.assertEqual((res["folded"], res["left"]), (5, 0))
        pages = self.tmp / "wiki/pages"
        self.assertIn("alpha deploys with make", (pages / "agent-notes-alpha.md").read_text())
        self.assertIn("agent-notes-alpha", self.wiki("index", "--cwd", self.alpha).stdout)
        self.assertNotIn("agent-notes-alpha", self.wiki("index", "--cwd", self.beta).stdout)
        page = (pages / "agent-notes.md").read_text()
        self.assertIn("reply in under ten lines", page)
        giraffes = sorted(f.read_text() for f in pages.glob("gated-notes-the-yellow-giraffe-*.md"))
        self.assertEqual(len(giraffes), 2)
        self.assertTrue("GIRAFFE ONE" in giraffes[0] and "GIRAFFE TWO" not in giraffes[0], giraffes[0])
        # the gated note went to a gated page without passing through the model
        self.assertNotIn("ZEBRA", page)
        self.assertEqual(self.j("search", "ZEBRA NOTE BODY", ok=(1,))["hits"], [])
        hit = self.j("search", "ZEBRA NOTE BODY", "--gated")["hits"]
        self.assertEqual(len(hit), 1)
        self.assertTrue(hit[0]["slug"].startswith("gated-notes-zebra-is-gated-"), hit)
        self.assertNotIn("ZEBRA NOTE BODY", self.j("context", "--cwd", self.alpha)["text"])
        self.assertEqual(self.j("lint", ok=(0, 3))["frontmatter"], [])
        self.assertEqual(self.j("notes", "--all"), [])
        self.assertNotIn("Recent notes", self.j("context", "--cwd", self.alpha)["text"])


class Sync(Base):
    def test_notes_travel_between_hosts(self):
        """The Mac folds; a second host only adds notes. Both sync through one remote without conflicts."""
        self.wiki("init")
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        mac = self.tmp / "wiki"
        self.wiki("sync", ok=(2,))  # no remote yet
        subprocess.run(["git", "-C", str(mac), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(mac), "push", "-q", "origin", "HEAD:main"], check=True)
        other = self.tmp / "wiki-other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        oenv = dict(self.env, WIKI_HOME=str(other), WIKI_STATE=str(self.tmp / "state-other"))

        def other_wiki(*args):
            r = subprocess.run([sys.executable, str(WIKI_PY), *args], env=oenv, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return r

        other_wiki("note", "--title", "From the other host", "--type", "reference", "--body", "other host fact")
        self.wiki("note", "--title", "From the Mac", "--type", "reference", "--body", "mac fact")
        other_wiki("sync")
        self.wiki("sync")
        self.assertEqual(len(self.j("notes", "--all")), 2)
        self.assertEqual(self.j("fold")["folded"], 2)
        self.wiki("sync")
        other_wiki("note", "--title", "Second", "--type", "reference", "--body", "written before the pull")
        other_wiki("sync")
        self.assertIn("other host fact", "".join(f.read_text() for f in (other / "pages").glob("agent-notes*.md")))
        left = json.loads(other_wiki("notes", "--all", "--json").stdout)
        self.assertEqual([x["title"] for x in left], ["Second"])  # folded notes are gone, the new one survives
        self.wiki("sync")
        self.assertEqual([x["title"] for x in self.j("notes", "--all")], ["Second"])

    def test_sync_after_a_killed_rebase_keeps_new_notes(self):
        self.wiki("init")

        def g(repo, *args):
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        mac, remote = self.tmp / "wiki", self.tmp / "remote2.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        (mac / "shared.md").write_text("base\n")
        g(mac, "add", "-A")
        g(mac, "commit", "-qm", "base")
        g(mac, "remote", "add", "origin", str(remote))
        g(mac, "remote", "set-url", "origin", str(remote))  # the class shares one wiki; another test set a remote
        g(mac, "push", "-q", "origin", "HEAD:main")
        other = self.tmp / "wiki-other2"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        # both sides change one file, so the Mac's pull stops in a conflicted rebase, as a killed sync would
        (other / "shared.md").write_text("theirs\n")
        g(other, "commit", "-qam", "theirs")
        g(other, "push", "-q", "origin", "HEAD:main")
        (mac / "shared.md").write_text("ours\n")
        g(mac, "commit", "-qam", "ours")
        self.assertNotEqual(g(mac, "pull", "--rebase", "-q", "origin", "main").returncode, 0)
        self.assertTrue((mac / ".git/rebase-merge").exists() or (mac / ".git/rebase-apply").exists())
        self.wiki("note", "--title", "Saved mid-rebase", "--type", "user", "--body", "must survive")
        self.wiki("sync", ok=(1,))  # the conflict is still there, so the pull fails again and is aborted
        self.assertIn("Saved mid-rebase", [x["title"] for x in self.j("notes", "--all")])
        self.assertEqual(g(mac, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")
        self.assertEqual((mac / "shared.md").read_text(), "ours\n")

    def test_sync_skips_while_locked(self):
        self.wiki("init")
        got = self.py("import subprocess, sys\n"
                      "with wiki.Lock():\n"
                      f"    r = subprocess.run([sys.executable, {str(WIKI_PY)!r}, 'sync'], capture_output=True)\n"
                      "print(json.dumps(r.returncode))")
        self.assertIn(got, (2, 4))


class Public(Base):
    """Setup and no-model verbs."""

    def test_page_note_verify_doctor_install_nightly(self):
        self.assertEqual(self.j("import", "--all")["coverage"], 100.0)
        plan = self.wiki("import", "--refresh-all", "--dry-run").stdout
        self.memory(self.beta, "beta-new.md", "---\nname: n\ndescription: d\n---\nrun `beta deploy --prod` since 2026-01-02, "
                    "then `beta check all`, `beta logs -f` and `beta rollback now`\n")
        est = self.wiki("import", "--refresh-all", "--dry-run").stdout
        self.assertIn("estimate for the", est)
        self.assertNotIn("estimate for the", plan)  # nothing left to do, nothing to estimate

        # the new file is not in any page yet: verify shows its details as missing, then found after a refresh
        before = self.j("verify", "--project", self.beta)
        self.assertEqual((before["details"], before["found"]), (5, 0))
        self.assertEqual(self.j("import", "--refresh-all")["failed"], [])

        # a page note lands verbatim, without a model, and is committed
        calls = self.tmp / "calls.txt"
        env = dict(self.env, FAKE_OMP_CALLS=str(calls))
        r = subprocess.run([sys.executable, str(WIKI_PY), "note", "--type", "project", "--title", "Deploy flag",
                            "--page", "beta", "--body", "use `beta deploy --prod` only"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        page = (self.tmp / "wiki/pages/beta.md").read_text()
        self.assertIn("## Deploy flag (", page)
        self.assertIn("use `beta deploy --prod` only", page)
        self.assertFalse(calls.exists())
        self.assertEqual(git(self.tmp / "wiki", "status", "--porcelain"), "")
        self.assertEqual(self.j("notes", "--all"), [])
        self.wiki("note", "--type", "project", "--title", "x", "--page", "no-such-page", "--body", "y", ok=(2,))
        gated = next(f.stem for f in (self.tmp / "wiki/pages").glob("gated-*.md"))
        self.wiki("note", "--type", "project", "--title", "x", "--page", gated, "--body", "y", ok=(2,))
        # while a model pass holds the lock the note waits in the inbox and names its page
        got = self.py("import subprocess, sys\n"
                      "with wiki.Lock():\n"
                      f"    r = subprocess.run([sys.executable, {str(WIKI_PY)!r}, 'note', '--type', 'project', '--title', "
                      "'Later', '--page', 'beta', '--body', 'queued fact'], capture_output=True, text=True)\n"
                      "print(json.dumps(r.returncode))")
        self.assertEqual(got, 0)
        self.assertIn("page: beta", next((self.tmp / "wiki/inbox").glob("_agent--*.md")).read_text())

        # install: the Claude Code hook is added once, keeps other settings, and comes out again
        settings = self.home / ".claude/settings.json"
        settings.write_text(json.dumps({"model": "x", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
        self.wiki("install", "claude-code", "omp")
        self.wiki("install", "claude-code")
        got = json.loads(settings.read_text())
        self.assertEqual(got["model"], "x")
        cmds = [h["command"] for e in got["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertEqual(len(cmds), 2)
        self.assertIn('context --cwd "$CLAUDE_PROJECT_DIR" --instructions', cmds[1])
        ext = (self.home / ".omp/agent/extensions/wiki-memory.ts").read_text()
        self.assertNotIn("__WIKI_COMMAND__", ext)
        self.assertIn(json.dumps(str(WIKI_PY)), ext)
        doc = {c["check"]: c for c in self.j("doctor")["checks"]}
        self.assertEqual(doc["session hook"]["detail"], "Claude Code, omp")
        self.assertTrue(doc["wiki"]["ok"] and doc["model runner"]["ok"])
        self.assertEqual(doc["pending notes"]["detail"].split(";")[0][:1], "1")
        # another command sharing our entry survives the removal; the file keeps its mode
        got["hooks"]["SessionStart"][1]["hooks"].append({"type": "command", "command": "echo neighbour"})
        settings.write_text(json.dumps(got))
        os.chmod(settings, 0o600)
        self.wiki("install", "claude-code", "omp", "--remove")
        self.assertEqual([h["command"] for e in json.loads(settings.read_text())["hooks"]["SessionStart"] for h in e["hooks"]],
                         ["echo hi", "echo neighbour"])
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        settings.write_text("{not json")
        self.wiki("install", "claude-code", ok=(2,))
        self.assertEqual(settings.read_text(), "{not json")
        settings.write_text("{}")
        # someone else's `wiki` is never replaced
        other = self.home / ".local/bin/wiki"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.symlink_to("/usr/bin/true")
        self.wiki("install", "bin", ok=(2,))
        self.assertEqual(os.readlink(other), "/usr/bin/true")
        other.unlink()
        # a notes-only host queues a page note instead of editing the page
        r = subprocess.run([sys.executable, str(WIKI_PY), "note", "--type", "project", "--title", "From a notes host",
                            "--page", "beta", "--body", "queued too"], env=dict(self.env, WIKI_NOTES_ONLY="1"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("queued too", (self.tmp / "wiki/pages/beta.md").read_text())
        self.wiki("install", "schedule", "--at", "25:00", ok=(2,))
        # doctor fails without a runner
        r = subprocess.run([sys.executable, str(WIKI_PY), "doctor"], env=dict(self.env, WIKI_RUNNER="nope"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)

        # nightly: refresh (nothing stale) and fold the queued note
        res = self.j("nightly")
        self.assertEqual([(x["step"], x["rc"]) for x in res["steps"]], [("import --refresh-all", 0), ("fold", 0)])
        self.assertEqual(self.j("notes", "--all"), [])

    def test_runner_commands(self):
        code = ("from pathlib import Path\n"
                "print(json.dumps(wiki.runner_command('PROMPT', Path('/w'), '5m')))")
        self.assertEqual(self.py(code, WIKI_RUNNER="claude", WIKI_MODEL="haiku"),
                         ["claude", "-p", "PROMPT", "--dangerously-skip-permissions", "--model", "haiku"])
        self.assertEqual(self.py(code, WIKI_RUNNER="codex", WIKI_MODEL=""),
                         ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                          "-C", "/w", "PROMPT"])
        self.assertEqual(self.py(code, WIKI_RUNNER="", WIKI_RUNNER_CMD="agent --dir {cwd} --ask {prompt}"),
                         ["agent", "--dir", "/w", "--ask", "PROMPT"])
        self.assertEqual(self.py("print(json.dumps([wiki.seconds(x) for x in ('50m', '2h', '90')]))"), [3000, 7200, 90])
        # a quoted placeholder in the env file keeps its inner quotes
        cfg = self.tmp / "config-quotes"
        cfg.mkdir()
        (cfg / "env").write_text('WIKI_RUNNER_CMD=agent --ask "{prompt}"\n')
        env = {k: v for k, v in self.env.items() if k not in ("WIKI_RUNNER", "WIKI_RUNNER_CMD")}
        r = subprocess.run([sys.executable, "-c", f"import sys, json; sys.path.insert(0, {str(CODE)!r}); import wiki; "
                            "from pathlib import Path; print(json.dumps(wiki.runner_command('a b', Path('/w'), '5m')))"],
                           env=dict(env, WIKI_CONFIG=str(cfg)), capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout), ["agent", "--ask", "a b"])
        # the state dir may not sit inside a protected path: its scratch copies would make that path writable
        got = self.py("try:\n    wiki.sandbox_prefix()\n    print(json.dumps('started'))\n"
                      "except RuntimeError as e:\n    print(json.dumps('overlaps' in str(e)))",
                      WIKI_STATE=str(Path(self.env["WIKI_HOME"]) / "state"), WIKI_SANDBOX="1")
        self.assertIs(got, True)

    def test_env_file(self):
        cfg = self.tmp / "config-env"
        cfg.mkdir()
        (cfg / "env").write_text("# comment\nWIKI_MODEL=from-file\nWIKI_RUNNER=codex\nOTHER=x\n")
        got = self.py("import os\nprint(json.dumps([wiki.MODEL, wiki.RUNNER, os.environ.get('OTHER')]))",
                      WIKI_CONFIG=str(cfg), WIKI_MODEL="")
        self.assertEqual(got, ["", "omp", None])  # the real environment wins, even when empty; other keys are ignored
        env = {k: v for k, v in self.env.items() if k not in ("WIKI_MODEL", "WIKI_RUNNER")}
        r = subprocess.run([sys.executable, "-c", f"import sys, json; sys.path.insert(0, {str(CODE)!r}); import wiki; "
                            "print(json.dumps([wiki.MODEL, wiki.RUNNER]))"], env=dict(env, WIKI_CONFIG=str(cfg)),
                           capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout), ["from-file", "codex"])


class Units(Base):
    def test_gate_detection(self):
        d = self.tmp / "gate"
        d.mkdir()
        cases = {
            "folded.md": ("---\nname: x\ndescription: >-\n  only when asked. DO NOT\n  READ UNLESS the user mentions KIWI\n"
                          "---\nbody\n", "the user mentions KIWI"),
            "plain-continued.md": ("---\nname: x\ndescription: first line\n  do not read unless MANGO\n---\nbody\n",
                                   "MANGO"),
            "do-not-read-unless-plum.md": ("---\nname: x\ndescription: y\n---\nbody\n", "plum"),
            "broken.md": ("---\nname: x\nnot yaml at all\n---\nDO NOT READ UNLESS FIG\n", "FIG"),
            "broken-wrapped.md": ("---\nname: x\nnot yaml at all\n---\nDO NOT READ\nUNLESS FIG\n", "FIG"),
            "no-frontmatter.md": ("# title\nDO NOT READ UNLESS LIME\n", "LIME"),
            "body-only.md": ("---\nname: x\ndescription: y\n---\nsays do not read unless in the body\n", None),
        }
        for name, (text, _) in cases.items():
            (d / name).write_text(text)
        got = self.py(f"from pathlib import Path\nprint(json.dumps({{f.name: wiki.gate_trigger(f) "
                      f"for f in Path({str(d)!r}).glob('*.md')}}))")
        self.assertEqual(got, {name: trig for name, (_, trig) in cases.items()})

    def test_provider_limit_lines(self):
        cases = {
            "omp": ('Working...\n429 {"type":"error","error":{"type":"rate_limit_error","message":"slow down"}} '
                    "retry-after-ms=60000\n", 1, True),
            "plain": ("Working...\n429 Rate limit exceeded\n", 1, True),
            "status_code": ('{"statusCode":429,"message":"Rate limit exceeded"}\n', 1, True),
            "quota": ("402 Payment Required: usage limit reached\n", 1, True),
            "quoted": ("the docs say HTTP 429 means rate_limit_error; we retry on it\nError: tool failed\n", 1, False),
            "clean_exit": ("429 Rate limit exceeded\n", 0, False),
        }
        logs = self.tmp / "limit-logs"
        logs.mkdir()
        for name, (text, _, _) in cases.items():
            (logs / name).write_text(text)
        rcs = {name: rc for name, (_, rc, _) in cases.items()}
        got = self.py(f"from pathlib import Path\nout = {{}}\nfor n, rc in {rcs!r}.items():\n"
                      f"    wiki.LIMIT.clear()\n    out[n] = wiki.check_limit(rc, Path({str(logs)!r}) / n)\n"
                      "print(json.dumps({'out': out, 'retry': len(wiki._LIMIT_AT)}))")
        self.assertEqual(got["out"], {name: hit for name, (_, _, hit) in cases.items()})
        self.assertEqual(got["retry"], 1)  # only the omp line carries a retry-after

    def test_no_spawn_after_limit(self):
        calls = self.tmp / "spawn-calls"
        got = self.py("wiki.init_repo()\nwork = wiki.STATE / 'work/gate'\nwork.mkdir(parents=True, exist_ok=True)\n"
                      "wiki.LIMIT.set()\n"
                      "print(json.dumps(wiki.run_model('x', work, wiki.STATE / 'logs/gate.log', '1m', stoppable=True)))",
                      FAKE_OMP_CALLS=str(calls))
        self.assertEqual(got, [None, 0.0])
        self.assertFalse(calls.exists())

    def test_gated_slug_collision(self):
        self.memory(self.beta, "DO NOT READ UNLESS ZEBRA IS MENTIONED.md", "---\nname: z\ndescription: g\n---\nZEBRA 2\n")
        got = self.py("wiki.init_repo()\nfirst = wiki.copy_gated(wiki.source_projects())\n"
                      "again = wiki.copy_gated(wiki.source_projects())\n"
                      "print(json.dumps({'first': first, 'again': again, 'pages': {s: p.sources for s, p in "
                      "wiki.load_pages().items() if p.gated}}))")
        self.assertEqual(len(got["first"]), 2)
        self.assertEqual(got["again"], [])  # stable: a second pass finds each file's own page
        self.assertEqual(sorted(len(s) for s in got["pages"].values()), [1, 1])
        self.assertEqual(len({s[0] for s in got["pages"].values()}), 2)
        self.assertIn("gated-zebra-is-mentioned", got["pages"])

    def test_exclusion_boundaries(self):
        root = self.home / "Development/projects"  # the fixture excludes root/secret
        for d in ("secret-tools", "secretkit", "secret/sub"):
            (root / d).mkdir(parents=True)
        for d in ("secret-tools", "secretkit", "secret/sub", "secret-gone"):
            self.memory(str(root / d), "m.md", "---\nname: m\ndescription: d\n---\nfact\n", session=False)
        got = self.py("print(json.dumps([p['slug'] for p in wiki.source_projects()]))")
        self.assertIn(slug(str(root / "secret-tools")), got)  # an existing sibling, not below secret
        self.assertIn(slug(str(root / "secretkit")), got)
        self.assertNotIn(slug(str(root / "secret/sub")), got)
        self.assertNotIn(slug(str(root / "secret-gone")), got)  # no session, no dir: the slug decides, closed
        self.assertNotIn(slug(str(root / "secret")), got)

    def test_conflict_notes_unique(self):
        got = self.py("wiki.init_repo()\np = wiki.PAGES / 'shared.md'\np.write_text('---\\ntitle: Shared\\n---\\nlive\\n')\n"
                      "for i in range(2):\n"
                      "    work, base = wiki.prepare_work('w%d' % i)\n"
                      "    p.write_text(p.read_text() + 'moved on %d\\n' % i)\n"
                      "    (work / 'wiki/pages/shared.md').write_text('---\\ntitle: Shared\\n---\\nrun %d\\n' % i)\n"
                      "    wiki.integrate(work, base, 'same-tag')\n"
                      "print(json.dumps(sorted(f.name for f in wiki.INBOX.glob('shared--*.md'))))")
        self.assertEqual(got, ["shared--same-tag-2.md", "shared--same-tag.md"])

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "macOS sandbox-exec")
    def test_sandbox_and_redaction(self):
        for d in (self.home / ".claude/projects/-probe-proj/memory", self.home / ".omp/agent", self.home / "dotfiles"):
            d.mkdir(parents=True, exist_ok=True)
        (self.home / "dotfiles/AGENTS.md").write_text("rules\n")
        (self.home / ".omp/agent/AGENTS.md").symlink_to(self.home / "dotfiles/AGENTS.md")
        code = ("wiki.init_repo()\nwork = wiki.STATE / 'work/probe'\nwork.mkdir(parents=True, exist_ok=True)\n"
                "rc, _ = wiki.run_model('probe', work, wiki.STATE / 'logs/probe.log', '1m')\n"
                "s = next((wiki.STATE / 'sessions').glob('*_probe.jsonl')).read_text()\n"
                "print(json.dumps({'rc': rc, 'log': (wiki.STATE / 'logs/probe.log').read_text(), 'session': s}))")
        probe = f"{sys.executable} {CODE / 'tests/sandbox_probe.py'}"
        got = self.py(code, WIKI_OMP=probe)
        for name in ("projects", "claude_md", "agents_md", "agents_md_unlink", "claude_rename", "omp_agent_rename",
                     "wiki_git"):
            self.assertIn(f"probe {name} denied", got["log"])
        for name in ("scratch", "sessions", "omp_state", "transcript"):  # a runner's own transcript stays writable
            self.assertIn(f"probe {name} written", got["log"])
        self.assertEqual((self.home / "dotfiles/AGENTS.md").read_text(), "rules\n")
        self.assertTrue((self.home / ".omp/agent/AGENTS.md").is_symlink())
        for text in (got["log"], got["session"]):
            self.assertIn("[REDACTED-SECRET]", text)
            for leak in ("ghp_aaaa", "sk-bbbb", "b3BlbnNzaC1rZXktdjEAAAAA", "AAAAAAAAAA==", "END OPENSSH"):
                self.assertNotIn(leak, text)
        for line in got["session"].splitlines():
            json.loads(line)  # redaction keeps the JSONL valid
        off = self.py(code, WIKI_OMP=probe, WIKI_SANDBOX="0")
        self.assertIn("probe projects written", off["log"])


if __name__ == "__main__":
    unittest.main()
