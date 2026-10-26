# Prepared image for item citty-did-you-mean: agent-server + unjs/citty dev dependencies at the base commit.
# Dependencies live in /node_modules (Node resolves it from any checkout path: /w, /workspace/project).
FROM ghcr.io/openhands/agent-server:1.58.2-python
USER root
ARG REPO=https://github.com/unjs/citty.git
ARG BASE=3e342fe7b3f67575faf51df8361d502be6fe32e8
RUN npm install -g pnpm@11.22.0 \
 && git clone -q "$REPO" /tmp/src && git -C /tmp/src checkout -q "$BASE" \
 && mkdir -p /opt/deps && cp /tmp/src/package.json /tmp/src/pnpm-lock.yaml /tmp/src/pnpm-workspace.yaml /opt/deps/ \
 && cd /opt/deps && pnpm install --frozen-lockfile --ignore-scripts \
 && mv /opt/deps/node_modules /node_modules && chmod 1777 /node_modules \
 && rm -rf /tmp/src /root/.cache /root/.local/share/pnpm /root/.npm
USER openhands
