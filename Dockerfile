FROM python:3.12-slim

# Required: directory name of the plugin to serve, e.g. text-metrics.
ARG PLUGIN
# Optional space-separated apt packages (pdf-optimizer needs
# "ocrmypdf tesseract-ocr tesseract-ocr-eng tesseract-ocr-ukr ghostscript").
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

RUN useradd --system --no-create-home plugin
USER plugin

ENV PATH="/app/.venv/bin:$PATH" PLUGIN=${PLUGIN}
EXPOSE 8000
# Run from /app so "text-metrics.app" imports as a namespace package,
# the same way the README's `uvicorn webhook-forwarder.app:app` does.
CMD ["sh", "-c", "exec uvicorn \"$PLUGIN.app:app\" --host 0.0.0.0 --port 8000"]
