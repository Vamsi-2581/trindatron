#!/bin/bash
# Entrypoint for the trinity-desktop-sandbox container.
# Starts Xvfb on :99, launches fluxbox, and keeps the container alive.
# Traps SIGTERM/INT to cleanly kill all background processes on docker stop.
trap "kill 0" TERM INT

# Create empty Xauthority so Xlib clients don't fail trying to read ~/.Xauthority.
# Xvfb runs with -ac (no access control), so no real auth entry is needed.
touch /tmp/.Xauthority

Xvfb :99 -screen 0 1280x800x24 -ac -noreset &

# Wait for Xvfb to accept connections before starting the window manager
until xdpyinfo -display :99 >/dev/null 2>&1; do
    sleep 0.1
done

DISPLAY=:99 fluxbox -log /tmp/fluxbox.log &

wait
