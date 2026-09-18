#!/usr/bin/env bash
# Start a per-user PulseAudio daemon so Chrome has an audio sink, then exec the
# connector. Mirrors the parent repository's entrypoint.sh, minus the parts that
# only the Django services need.
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/xdg-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
export PULSE_RUNTIME_PATH="$XDG_RUNTIME_DIR/pulse"
mkdir -p "$PULSE_RUNTIME_PATH"

# Point ALSA's default device at PulseAudio.
cat > "$HOME/.asoundrc" <<'ASOUND'
pcm.!default { type pulse }
ctl.!default { type pulse }
ASOUND

if [[ -z "${PULSE_SERVER:-}" ]]; then
  rm -f "$PULSE_RUNTIME_PATH/pid"
  pulseaudio --daemonize=yes --exit-idle-time=-1 --realtime=no \
             --high-priority=no --log-target=stderr --disallow-exit
  export PULSE_SERVER="unix:${PULSE_RUNTIME_PATH}/native"
fi

for _ in {1..50}; do pactl info >/dev/null 2>&1 && break; sleep 0.1; done
pactl info >/dev/null || { echo "FATAL: pactl cannot reach PulseAudio" >&2; exit 1; }

# Use the null sink so audio playback always has somewhere to go.
if pactl list short sinks | awk '{print $2}' | grep -qx auto_null; then
  pactl set-default-sink auto_null || true
  pactl set-default-source auto_null.monitor || true
fi

exec "$@"
