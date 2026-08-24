# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Sanguine Sentry is an auto-flask calibration and health-globe monitoring utility for action RPGs (Path of Exile, etc.) on Linux/Windows: it watches a cropped screen region, computes health % (percent-ratio scan or OpenCV template matching), and triggers keybinds/commands via `pynput` when thresholds are crossed. Ships with a fail-safe "town/loading gate" (a second pixel check that suspends triggering outside combat) and a local web dashboard for calibration/telemetry. See `README.md` for the full feature/config-key list.

**Note:** README says `python server.py` is the entrypoint — actual entrypoint is `api/server.py` (root `monitor.py` also has its own `if __name__ == "__main__"` block; `api/server.py` is what wires the dashboard + monitor together).

## Architecture

Three components:

- **`core/`** — the monitoring engine, split by concern: `scanner.py` (NumPy-vectorized pixel/globe analysis), `trigger.py` (pynput/xdotool action firing), `config.py`, `ocr.py`, `llm.py` (optional Ollama-based threshold auto-tuning — hits a hardcoded LAN Ollama host at `<ollama-host>:11434`, model `llama3.1`/`llava`, not the local default). `monitor.py` at the root composes these into the monitoring daemon/loop.
- **`api/server.py`** — HTTP API + serves the dashboard (`api/web/`), binds `127.0.0.1:8080` by default (see `bind_ip`/`port` in `config.json`). Has startup auth tokens and `Host` header verification against DNS rebinding — this is a locally-run tool with sensitive I/O access (simulated input, arbitrary configured OS commands via `xdotool`), so don't loosen the auth/rebinder checks without a clear reason.
- **`sanguine_wayland_capture/`** — separate Rust crate, Pipewire/D-Bus-portal screen capture daemon for Wayland sessions (X11/Windows fall back to `mss` in Python). Python spawns it and talks over a local socket when a Wayland session is detected. Built/released independently via `.github/workflows/release.yml` (`cargo build --release`, tagged `v*` triggers a GitHub release upload) — it is not built as part of the normal Python dev loop.
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

# Rust capture daemon (Wayland only, optional)
cd sanguine_wayland_capture && cargo build --release
```

No lint/format tooling is configured in this repo (no ruff/black/flake8 config present) — match surrounding style.

`scratch/` holds ad hoc exploration scripts (e.g. LLaVA bbox testing) — not part of the app, don't treat it as source of truth for behavior.
