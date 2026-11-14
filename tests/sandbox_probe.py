#!/usr/bin/env python3
"""Stand-in for `omp -p` in the sandbox test: tries writes the sandbox must deny and ones it must allow,
and leaves secrets in its output and session file for the post-run redaction."""

import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
cwd = Path(argv[argv.index("--cwd") + 1])
sessions = Path(argv[argv.index("--session-dir") + 1])
home = Path(os.environ["HOME"])


def probe(name, act):
    try:
        act()
        print(f"probe {name} written")
    except OSError as e:
        print(f"probe {name} denied ({e.errno})")


def move_and_back(src: Path):
    def act():
        moved = src.with_name(src.name + "-moved")
        src.rename(moved)
        moved.rename(src)  # put it back, so a run without the sandbox leaves the fixture whole
    return act


agents_md = home / ".omp/agent/AGENTS.md"  # a symlink in the test
probe("projects", lambda: (home / ".claude/projects/-probe-proj/memory/probe.md").write_text("x"))
probe("transcript", lambda: (home / ".claude/projects/-probe-proj/session.jsonl").write_text("x"))
probe("claude_md", lambda: (home / ".claude/CLAUDE.md").write_text("x"))
probe("agents_md", lambda: agents_md.write_text("x"))
probe("agents_md_unlink", lambda: (agents_md.unlink(), agents_md.symlink_to(home / "dotfiles/AGENTS.md")))
probe("claude_rename", move_and_back(home / ".claude"))
probe("omp_agent_rename", move_and_back(home / ".omp/agent"))
probe("wiki_git", lambda: (Path(os.environ["WIKI_HOME"]) / ".git/probe").write_text("x"))
probe("scratch", lambda: (cwd / "allowed.txt").write_text("x"))
probe("sessions", lambda: (sessions / "probe.txt").write_text("x"))
probe("omp_state", lambda: (home / ".omp/agent/probe-state.db").write_text("x"))
pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nAAAAAAAAAA==\n-----END OPENSSH PRIVATE KEY-----"
print("token ghp_" + "a" * 30)
print(pem)
(sessions / "2026-01-01T00-00-00-000Z_probe.jsonl").write_text(
    json.dumps({"type": "session", "cwd": str(cwd)}) + "\n"
    + json.dumps({"type": "message", "text": "key sk-" + "b" * 30 + " and " + pem}) + "\n")
