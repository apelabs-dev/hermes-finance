#!/bin/sh
# Install this repo's git hooks (scripts/hooks/: the privacy pre-commit, scripts/dehardcode.py) by pointing core.hooksPath at them.
# core.hooksPath lives in the repository's shared config, so it applies to every worktree of this clone.
# Undo: git config --unset core.hooksPath
set -e
root=$(git rev-parse --show-toplevel)
chmod +x "$root/scripts/hooks/pre-commit"
git -C "$root" config core.hooksPath scripts/hooks
echo "core.hooksPath = scripts/hooks (pre-commit: the word check on staged src/ and hermes_plugin/ files, then the release scan of every staged file that ships)"
