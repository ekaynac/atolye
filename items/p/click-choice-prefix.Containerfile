# Prepared image for item click-choice-prefix: agent-server + pallets/click test dependencies at the base commit.
# The repository itself is NOT installed; tests import it from the checkout via PYTHONPATH=src.
FROM ghcr.io/openhands/agent-server:1.58.2-python
USER root
# tests/test_utils/test_echo_via_pager.py runs the real `less` pager.
RUN apt-get update && apt-get install -y --no-install-recommends less && rm -rf /var/lib/apt/lists/*
# pytest version pinned by click's uv.lock at base 2247b35ea1c47c727d7a06e51fa280e12a863ff6
RUN pip install --no-cache-dir "pytest==9.0.2" && python -m pytest --version
# tests/test_deprecations.py reads importlib.metadata.version("click"). Register metadata only (no code), with the
# base commit's version, so the checkout's src/ stays the only importable click.
RUN d="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/click-8.6.0.dev0.dist-info" \
 && mkdir -p "$d" && printf 'Metadata-Version: 2.1\nName: click\nVersion: 8.6.0.dev0\n' > "$d/METADATA" \
 && python -c 'import importlib.metadata as m; print("click metadata", m.version("click"))'
USER openhands
