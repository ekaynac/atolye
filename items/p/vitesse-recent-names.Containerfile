# Prepared image for item vitesse-recent-names: agent-server + antfu-collective/vitesse-lite dependencies at the base
# commit, plus Playwright with its Chromium build for asserted browser scenarios.
# Dependencies live in /node_modules (Node resolves it from any checkout path: /w, /workspace/project).
FROM ghcr.io/openhands/agent-server:1.58.2-python
USER root
ARG REPO=https://github.com/antfu-collective/vitesse-lite.git
ARG BASE=0b352977755e1f7f6399698e308777760409ca71
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npm install -g pnpm@12.3.4 \
 && git clone -q "$REPO" /tmp/src && git -C /tmp/src checkout -q "$BASE" \
 && mkdir -p /opt/deps && cp /tmp/src/package.json /tmp/src/pnpm-lock.yaml /tmp/src/pnpm-workspace.yaml /opt/deps/ \
 && cd /opt/deps && pnpm install --frozen-lockfile --ignore-scripts \
 && mv /opt/deps/node_modules /node_modules && chmod 1777 /node_modules \
 && rm -rf /tmp/src /root/.cache /root/.local/share/pnpm /root/.npm
RUN mkdir -p /opt/playwright && cd /opt/playwright && npm init -y >/dev/null \
 && npm install --no-audit --no-fund playwright@1.64.1 \
 && ./node_modules/.bin/playwright install chromium \
 && ln -s /opt/playwright/node_modules/playwright /node_modules/playwright \
 && chmod -R a+rX /opt/ms-playwright /opt/playwright && rm -rf /root/.npm
USER openhands
