# TRINITY AGENT sandbox image.
# Minimal Debian with common dev tools. No secrets, no host mounts, no network
# (enforced at runtime via --network=none). Runs as non-root user "sandbox".
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        findutils \
        grep \
        sed \
        gawk \
        python3 \
        nodejs \
        curl \
        wget \
        git \
        jq \
        file \
    && rm -rf /var/lib/apt/lists/*

# Non-root user that owns no host paths.
RUN useradd -m -u 1000 -s /bin/bash sandbox

USER sandbox
WORKDIR /workspace
