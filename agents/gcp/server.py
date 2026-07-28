"""GCP leg: a Google ADK agent on Gemini, served over A2A by ``to_a2a()``.

The only one of the three that does not touch the a2a-sdk serving scaffolding
-- ADK builds its own Starlette app. It is also the only one that gives no way
to configure the URL its card advertises: ``to_a2a(host, port)`` writes the
*bind* address into the card, so every remote client must rewrite it. That is
the interop finding this whole exercise started from, and it is left
un-patched here on purpose: the fix belongs in the client, and the matrix
should show which clients can express it.

    python -m agents.gcp.server            # direct mode, no credentials
    CURRENCY_MODEL_MODE=llm python -m agents.gcp.server     # Gemini

Environment: ``PORT`` (10001), ``GENAI_MODEL``, ``MCP_SERVER_URL``,
``CURRENCY_MODEL_MODE``, ``CURRENCY_RATE_PROVIDER``.
"""

import logging
import os
from collections.abc import AsyncGenerator

from starlette.responses import JSONResponse

from agents.common import DESCRIPTION, INSTRUCTION, deterministic_reply, model_mode

logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.INFO)

DEFAULT_PORT = 10001
AGENT_NAME = "currency_agent"


def _direct_agent():
    """A BaseAgent that answers without a model, so to_a2a() stays in the path."""
    from google.adk.agents import BaseAgent
    from google.adk.events import Event
    from google.genai import types

    class DirectCurrencyAgent(BaseAgent):
        async def _run_async_impl(self, ctx) -> AsyncGenerator:
            prompt = ""
            if ctx.user_content and ctx.user_content.parts:
                prompt = "\n".join(part.text or "" for part in ctx.user_content.parts)
            reply = await deterministic_reply(prompt)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=reply)]),
            )

    return DirectCurrencyAgent(name=AGENT_NAME, description=DESCRIPTION)


def _llm_agent():
    """Native brain: Gemini with the exchange rate reached over MCP."""
    from google.adk.agents import LlmAgent
    from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

    return LlmAgent(
        model=os.getenv("GENAI_MODEL", "gemini-2.5-flash"),
        name=AGENT_NAME,
        description=DESCRIPTION,
        instruction=INSTRUCTION,
        tools=[
            McpToolset(
                connection_params=StreamableHTTPConnectionParams(
                    url=os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8081/mcp")
                ),
            ),
        ],
    )


def build():
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    root_agent = _llm_agent() if model_mode() == "llm" else _direct_agent()
    a2a_app = to_a2a(
        root_agent,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
    )

    async def health(request):
        return JSONResponse({"status": "ok", "agent": AGENT_NAME})

    a2a_app.add_route("/health", health, methods=["GET"])
    return a2a_app


app = build()


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
    )


if __name__ == "__main__":
    main()
