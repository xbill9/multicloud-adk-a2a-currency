"""AWS leg: a Strands agent on Bedrock, served over A2A.

Strands ships no A2A server integration, so this agent sits directly on the
``a2a-sdk`` reference routes. That makes it the control column of the interop
matrix: a cell that fails here is a client-side wire problem, because the
server is the protocol's own reference implementation.

    python -m agents.aws.server            # direct mode, no credentials
    CURRENCY_MODEL_MODE=llm python -m agents.aws.server   # Strands on Bedrock

Environment: ``PORT`` (10002), ``PUBLIC_URL``, ``BEDROCK_MODEL_ID``,
``CURRENCY_MODEL_MODE``, ``CURRENCY_RATE_PROVIDER``.
"""

import os

from agents.common import INSTRUCTION, deterministic_reply, model_mode, public_url, rate_provider
from agents.serving import CallbackExecutor, build_agent_card, build_app

DEFAULT_PORT = 10002
AGENT_NAME = "currency_agent"


def _strands_responder():
    """Native brain: Strands + Bedrock, with the rate lookup as a Strands tool."""
    from strands import Agent, tool
    from strands.models import BedrockModel

    provider = rate_provider()

    @tool
    async def get_exchange_rate(currency_from: str, currency_to: str) -> dict:
        """Get the current exchange rate between two currencies.

        Args:
            currency_from: The currency to convert from (e.g. "USD").
            currency_to: The currency to convert to (e.g. "EUR").
        """
        rate, observed_at = await provider.get_rate(currency_from.upper(), currency_to.upper())
        return {
            "from": currency_from.upper(),
            "to": currency_to.upper(),
            "rate": str(rate),
            "observed_at": observed_at.isoformat(),
        }

    agent = Agent(
        model=BedrockModel(model_id=os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-micro-v1:0")),
        system_prompt=INSTRUCTION,
        tools=[get_exchange_rate],
    )

    async def respond(prompt: str) -> str:
        return str(await agent.invoke_async(prompt))

    return respond


def build() -> tuple:
    responder = _strands_responder() if model_mode() == "llm" else deterministic_reply
    card = build_agent_card(name=AGENT_NAME, url=public_url(DEFAULT_PORT))
    return build_app(CallbackExecutor(responder), card), card


app, _card = build()


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
    )


if __name__ == "__main__":
    main()
