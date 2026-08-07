# One image, two entrypoints: the agent runs as a Cloud Run service, the
# coordinator as a Cloud Run job. Their dependency sets overlap almost
# entirely, and a single image means the deployed coordinator is provably the
# same code as the deployed agent.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Unpinned deliberately. google-adk 2.4.0 could not serve A2A v1.0 (finding 4
# in docs/INTEROP.md), but that was a fact about 2.4.0, not a law: retested
# 2026-08-02 against google-adk 2.6.1 + a2a-sdk 1.1.2 and to_a2a imports and
# serves. A pin nobody retests is indistinguishable from rot.
RUN pip install \
      "a2a-sdk[http-server]" \
      google-adk \
      agent-framework-a2a \
      agent-framework-core \
      pydantic \
      httpx \
      uvicorn

COPY pyproject.toml README.md ./
COPY agents ./agents
COPY clients ./clients
COPY coordinator ./coordinator
COPY matrix ./matrix
COPY protocol ./protocol
COPY mcp_server ./mcp_server
COPY evaluations ./evaluations

RUN pip install --no-deps -e .

# Cloud Run routes to $PORT and requires a non-loopback bind. The agent
# defaults HOST to 127.0.0.1 for local runs, so it must be set here --
# a container listening on loopback fails Cloud Run's health check with a
# message that says nothing about the bind address.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    CURRENCY_MODEL_MODE=direct

CMD ["python", "-m", "agents.gcp.server"]
