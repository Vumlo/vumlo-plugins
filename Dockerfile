FROM python:3.12-slim

# Required: directory name of the plugin to serve, e.g. text-metrics.
ARG PLUGIN
# Optional space-separated apt packages, normally the contents of <plugin>/apt-packages.txt
# (no plugin of the catalog needs any today).
ARG APT_PACKAGES=""

RUN test -n "$PLUGIN" || { echo "error: build arg PLUGIN is required, e.g. --build-arg PLUGIN=text-metrics" >&2; exit 1; }

RUN if [ -n "$APT_PACKAGES" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends $APT_PACKAGES \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY vumlo_plugins/ vumlo_plugins/
COPY ${PLUGIN}/ ${PLUGIN}/

RUN useradd --system --no-create-home plugin \
    && mkdir /data && chown plugin /data
USER plugin

# The plugin's small memory (SQLite state) lives in /data, which belongs to the user the image runs as. Mount a
# volume there to keep it across restarts; a named volume takes the ownership of this directory on first use.
ENV PATH="/app/.venv/bin:$PATH" PLUGIN=${PLUGIN} VUMLO_PLUGIN_DATA=/data
EXPOSE 8000
# Run from /app so "text-metrics.app" imports as a namespace package,
# the same way the README's `uvicorn webhook-forwarder.app:app` does.
# No access log: a request line carries the path, and a path can hold a token (the calendar feed's).
CMD ["sh", "-c", "exec uvicorn \"$PLUGIN.app:app\" --host 0.0.0.0 --port 8000 --no-access-log"]
