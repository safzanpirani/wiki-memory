#!/bin/sh
# Link `wiki` into ~/.local/bin, create the data repo, wire the agents on this machine and show what is left.
#   ./install.sh                 the CLI, an empty wiki, the skill, and every agent found here
#   ./install.sh claude-code     the CLI, an empty wiki, and only the named agents
#                                (claude-code, codex, droid, opencode, pi, omp, agy)
#   ./install.sh --dry-run       list what would be wired and change nothing
#   curl -fsSL https://raw.githubusercontent.com/safzanpirani/wiki-memory/main/install.sh | sh
#                                the same without a checkout: clones to ~/.local/share/wiki-memory/src first
#                                (WIKI_SRC picks another folder)
set -eu
command -v python3 >/dev/null || { echo "install: python3 (3.9 or newer) is required" >&2; exit 1; }
command -v git >/dev/null || { echo "install: git is required" >&2; exit 1; }
here=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
if [ ! -f "$here/wiki.py" ]; then  # piped into sh: there is no checkout beside this script
  here=${WIKI_SRC:-$HOME/.local/share/wiki-memory/src}
  if [ -d "$here/.git" ]; then
    git -C "$here" pull --quiet --ff-only
  else
    mkdir -p "$(dirname "$here")"
    git clone --quiet https://github.com/safzanpirani/wiki-memory "$here"
  fi
  echo "wiki-memory source: $here"
fi
case " $* " in
  *" --dry-run "*) exec python3 "$here/wiki.py" install "$@" ;;
esac
named=0
for arg in "$@"; do case "$arg" in -*) ;; *) named=1 ;; esac; done
python3 "$here/wiki.py" init
if [ "$named" = 1 ]; then
  python3 "$here/wiki.py" install bin "$@"
else
  python3 "$here/wiki.py" install "$@"
fi
python3 "$here/wiki.py" doctor || true
echo "Start a new agent session to load the memory. Running sessions keep what they started with."
