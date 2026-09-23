#!/usr/bin/env bash
# Drives api/server.py's portalgrab start/stop helpers against the real user service, then restores
# the service to how it was.
S=$(cd "$(dirname "$0")/.." && pwd)
was=$(systemctl --user is-active portalgrab)
trap '[ "$was" = active ] && systemctl --user start portalgrab || systemctl --user stop portalgrab' EXIT
cd "$S" || exit 1
py() { venv/bin/python -c "import api.server as s; from core.scanner import ScannerMixin; $1" 2>/dev/null; }

systemctl --user stop portalgrab
[ "$(py 'print(s.start_portalgrab(ScannerMixin()))')" = True ] || { echo "did not report starting it"; exit 1; }
systemctl --user is-active -q portalgrab || { echo "service not running after start"; exit 1; }
py 's.stop_portalgrab(True)'; sleep 0.5
systemctl --user is-active -q portalgrab && { echo "still running after stop"; exit 1; }

# Already running (started by someone else): we must neither claim nor stop it.
systemctl --user start portalgrab
for i in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/portalgrab.sock" ] && break; sleep 0.1; done
[ "$(py 'print(s.start_portalgrab(ScannerMixin()))')" = False ] || { echo "claimed a daemon it did not start"; exit 1; }
echo PORTALGRAB-LIFECYCLE-OK
