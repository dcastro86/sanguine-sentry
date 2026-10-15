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

# Ollama -> llama-swap (Phase 2b-A, 2026-10-14): llm.py talks /v1/chat/completions on the Unraid llama-swap
# (hermes3 -> deepseek-r1-8b, llava -> qwen3-vl, JSON via response_format json_schema). Ollama must get no call.
gate "no Ollama API left" \
  "! git grep -nE '11434|/api/(generate|chat|tags|ps|embed)' -- ':!GATES.sh' ':!*test*' ':!*.md'"

gate "threshold tuning and globe finding both answer live from llama-swap, with Ollama idle" \
  "for v in venv .venv ../sanguine-sentry/venv; do [ -x \$v/bin/python ] && break; done; \$v/bin/python tests/llm_live.py | grep -q '^LLM-LIVE-OK\$' && curl -sf http://192.168.50.71:11434/api/ps | jq -e '.models == []' >/dev/null"
