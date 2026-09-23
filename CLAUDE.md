# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal

One lifecycle.

### 1. The monitor (open-ended hobby project)

**For:** me, dcastro86: a screen-reading health monitor that fires a configured keybind or command when a
health globe drops below a threshold, calibrated from a local dashboard, for **offline or single-player
games, or testing**.

**Not for Path of Exile online, or any online game whose terms forbid input automation.** An automatic
trigger that reads the screen and presses keys on its own is the kind of third-party automation Path of
Exile's terms prohibit (one server action per keypress), so using it there risks an account ban.
PoEBuddy is the PoE tool, and it only advises. This scope was set 2026-09-15, although the README
originally named PoE as the primary target.

**Healthy when:** the test suite passes, and `api/server.py` starts the dashboard on `127.0.0.1:8080`
with the API token check and `Host`-header verification still in force. There is no "done": new features
are allowed without a scope change here. That is a deliberate choice (2026-09-15).

**Not doing:** targeting or advertising PoE, or any online game that forbids automation (above); binding
beyond `127.0.0.1` by default; loosening the token or DNS-rebinding checks; a frontend build step.

**Status:** not running. Auto-trigger is `enabled: false` in `config.json`, `debug.log` was last written
2026-07-18, and the last feature work was 2026-07-21. 9 tests pass. Wayland capture moved to portalgrab
2026-09-23.

## What this is

Sanguine Sentry is an auto-flask calibration and health-globe monitoring utility for action RPGs on Linux/Windows (offline or single-player only; not Path of Exile, see Goal): it watches a cropped screen region, computes health % (percent-ratio scan or OpenCV template matching), and triggers keybinds/commands via `pynput` when thresholds are crossed. Ships with a fail-safe "town/loading gate" (a second pixel check that suspends triggering outside combat) and a local web dashboard for calibration/telemetry. See `README.md` for the full feature/config-key list.

**Note:** the entrypoint is `api/server.py` (root `monitor.py` also has its own `if __name__ == "__main__"` block; `api/server.py` is what wires the dashboard + monitor together).

## Architecture

Three components:

- **`core/`** — the monitoring engine, split by concern: `scanner.py` (NumPy-vectorized pixel/globe analysis), `trigger.py` (pynput/xdotool action firing), `config.py`, `ocr.py`, `llm.py` (optional Ollama-based threshold auto-tuning — `OLLAMA_URL` env var, default `http://localhost:11434/api/generate`, model `llama3.1`/`llava`). `monitor.py` at the root composes these into the monitoring daemon/loop.
- **`api/server.py`** — HTTP API + serves the dashboard (`api/web/`), binds `127.0.0.1:8080` by default (see `bind_ip`/`port` in `config.json`). Has an API token (generated once with `secrets.token_hex(16)` when `config.json` has none, then persisted there and reused on later starts) and `Host` header verification against DNS rebinding — this is a locally-run tool with sensitive I/O access (simulated input, arbitrary configured OS commands via `xdotool`), so don't loosen the auth/rebinder checks without a clear reason.
- **Wayland capture** — external: [portalgrab](https://github.com/dcastro86/portalgrab), a separate repo and user service (spun out of this repo's old `sanguine_wayland_capture` crate on 2026-09-23). `core/scanner.py` talks to `$XDG_RUNTIME_DIR/portalgrab.sock` when it exists and falls back to `spectacle` otherwise; X11/Windows use `mss`. Python never spawns it.
- **`web/`** — dashboard frontend: vanilla HTML/CSS/JS, no build step, no framework.
- Config is read/written at runtime to `config.json` (see `config.json.example` for the schema/defaults — documented in README's Configuration table).

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Run
python api/server.py        # starts monitor + dashboard on :8080

# Tests (pytest + pytest-mock)
pytest
pytest tests/test_cv_matching.py -k <name>   # single test

```

No lint/format tooling is configured in this repo (no ruff/black/flake8 config present) — match surrounding style.

`scratch/` holds ad hoc exploration scripts (e.g. LLaVA bbox testing) — not part of the app, don't treat it as source of truth for behavior.
