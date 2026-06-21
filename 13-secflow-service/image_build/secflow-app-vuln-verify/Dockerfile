ARG SECFLOW_PI_AGENT_RUNTIME_IMAGE=ghcr.io/runshine/secflow-base-pi-agent-runtime:20260602
FROM ${SECFLOW_PI_AGENT_RUNTIME_IMAGE}

ARG SECFLOW_BUILD_VERSION=""
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/opt/vuln-verify/src:/opt/vuln-verify/packages/vuln-dispatch/src \
    PI_CODING_AGENT_DIR=/root/.pi/agent \
    PI_MODELS_JSON=/root/.pi/agent/models.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates procps psmisc findutils coreutils jq git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY vendor/vuln-verify /opt/vuln-verify
COPY skills/ /opt/secflow-skills/

COPY app/ /app/app/
COPY config.yaml /app/config.yaml
RUN printf '{"build_version":"%s"}\n' "$SECFLOW_BUILD_VERSION" > /app/build_meta.json

RUN mkdir -p /root/.pi/agent/bin /data/config /data/files /data/vuln-verify \
    && for _cmd in rg grep find head tail cat sed awk cut tr sort uniq wc xargs ls stat file basename dirname readlink realpath jq python3; do \
        _target="$(command -v "$_cmd" 2>/dev/null)" && [ -x "$_target" ] && ln -sf "$_target" "/root/.pi/agent/bin/$_cmd" 2>/dev/null || true; \
    done

COPY scripts/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/app/vuln-verify/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "app.main"]
