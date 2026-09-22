#!/usr/bin/env bash
set -euo pipefail

# Create a .venv from a specific Python version and install the package.
#
#   ./scripts/setup-venv.sh            # python 3.11 (project minimum)
#   ./scripts/setup-venv.sh 3.12       # exactly 3.12
#   VENV_DIR=/tmp/venv ./scripts/setup-venv.sh 3.13
#
# The interpreter is looked up, in order: python3.<v>/python<v> on PATH,
# pyenv versions, then Homebrew python@<v> (Apple Silicon then Intel).
# If the exact version is missing, the script stops with install hints.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT/.venv}"

if [[ $# -lt 1 || ! "$1" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "usage: $0 PYTHON_VERSION   e.g. 3.11 | 3.12 | 3.13" >&2
    exit 2
fi
MAJOR="${1%.*}"
MINOR="${1#*.}"

find_python() {
    local v="$1"
    if command -v "python3.$MINOR" >/dev/null 2>&1; then
        echo "python3.$MINOR"; return
    fi
    if command -v "python$v" >/dev/null 2>&1; then
        echo "python$v"; return
    fi

    local pyenv_root="$(pyenv root 2>/dev/null || true)"
    if [[ -n "$pyenv_root" && -d "$pyenv_root/versions/$v" ]]; then
        echo "$pyenv_root/versions/$v/bin/python3"; return
    fi

    local brew candidate
    for brew in /opt/homebrew /usr/local; do
        candidate="$brew/opt/python@$v/bin/python3.$MINOR"
        if [[ -x "$candidate" ]]; then
            echo "$candidate"; return
        fi
    done
    echo ""
}

PYTHON="$(find_python "$1")"
if [[ -z "$PYTHON" ]]; then
    cat >&2 <<EOF
$0: python $1 not found on this machine.

  brew install python@$1
    or
  pyenv install $1 && pyenv local $1
    or install from https://www.python.org/downloads/
EOF
    exit 1
fi

echo "using $PYTHON -> $("$PYTHON" --version 2>&1)"
echo "venv at $VENV_DIR"

"$PYTHON" -m venv "$VENV_DIR"

source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
pip install -e "$ROOT"

if [[ ! -f "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "created $ROOT/.env from .env.example"
fi

echo "done."
echo "activate with: source $VENV_DIR/bin/activate"