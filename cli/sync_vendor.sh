#!/usr/bin/env bash
# Re-syncs the vendored copy of lint_engine.py used by the CLI package from
# the real source of truth in ../backend. Run this whenever backend/lint_engine.py
# changes and before cutting a new CLI release.
set -euo pipefail
cd "$(dirname "$0")"

NOTE='"""
VENDORED COPY — synced from ../backend/lint_engine.py, NOT the source of
truth. Exists here only because a real (non-editable) `pip install` cannot
reach outside its own package directory to import a sibling repo folder —
confirmed by testing a real install, which crashed with ModuleNotFoundError
before this copy was added. Re-sync after any change to the real file with
`./sync_vendor.sh` in this directory. Do not hand-edit this copy.
"""
'

{ printf '%s\n' "$NOTE"; cat ../backend/lint_engine.py; } > lint_engine.py
echo "Synced cli/lint_engine.py from backend/lint_engine.py"
