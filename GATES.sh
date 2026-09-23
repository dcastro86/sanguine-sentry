#!/usr/bin/env bash
# Acceptance ledger for sanguine-sentry's use of portalgrab (github.com/dcastro86/portalgrab).
# Run with `gates` from the repo root. The lifecycle gate starts and stops the portalgrab user service.

gate "the test suite passes" \
  "venv/bin/python -m pytest -q 2>&1 | tail -1 | grep -qE '^[0-9]+ passed'"

gate "no in-repo capture crate or release workflow on origin/main" \
  "git fetch -q && ! git ls-tree -r --name-only origin/main | grep -qE '^(sanguine_wayland_capture/|\.github/workflows/release\.yml)'"

gate "scanner talks to portalgrab.sock" \
  "grep -q portalgrab.sock core/scanner.py && ! grep -q sanguine_sentry.sock core/scanner.py"

gate "README points at portalgrab, not the old crate" \
  "grep -q github.com/dcastro86/portalgrab README.md && ! grep -q sanguine_wayland_capture README.md"

gate "portalgrab is not started at login on this machine (the server starts it)" \
  "[ \"\$(systemctl --user is-enabled portalgrab 2>&1)\" = disabled ]"

gate "the server starts portalgrab on launch and stops only what it started" \
  "bash tests/portalgrab_lifecycle.sh | grep -q '^PORTALGRAB-LIFECYCLE-OK$'"
