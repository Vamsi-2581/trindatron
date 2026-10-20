FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Virtual framebuffer display server
    xvfb \
    x11-utils \
    # Minimal window manager
    fluxbox \
    # Screenshot capture
    scrot \
    # Python + X11 binding + pip for pyautogui
    python3 \
    python3-pip \
    python3-pil \
    python3-xlib \
    python3-tk \
    # Window listing (read-only; used by desktop_get_windows)
    wmctrl \
    # File opener (used by desktop_open)
    xdg-utils \
    # Fonts required for rendering
    fonts-dejavu-core \
    # Core tools (no curl/wget/git — network is off)
    bash coreutils findutils \
    && pip3 install --no-cache-dir --break-system-packages pyautogui \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Create sandbox user (uid 1000) — matches --user 1000:1000 in docker run
RUN useradd -m -u 1000 -s /bin/bash sandbox

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER sandbox
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
