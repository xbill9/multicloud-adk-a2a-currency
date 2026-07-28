"""Azure leg: a Microsoft Agent Framework agent on Foundry, served over A2A.

Unlike the AWS leg, this one does not sit on the reference executor: Agent
Framework ships its own ``A2AExecutor``, which converts between framework
Messages and A2A parts and drives the task lifecycle (submit / start_work /
complete) rather than replying with a single Message. That conversion layer is
what this column of the matrix tests, so it stays in the path in both brains
-- direct mode swaps the model for a stub agent, not the executor.

    python -m agents.azure.server          # direct mode, no credentials
    CURRENCY_MODEL_MODE=llm python -m agents.azure.server   # Foundry model

Environment: ``PORT`` (10003), ``PUBLIC_URL``, ``FOUNDRY_PROJECT_ENDPOINT``,
``AZURE_AI_MODEL_DEPLOYMENT_NAME``, ``CURRENCY_MODEL_MODE``.
"""

import os

from agents.common import INSTRUCTION, deterministic_reply, model_mode, public_url, rate_provider
from agents.serving import build_agent_card, build_app

DEFAULT_PORT = 10003
AGENT_NAME = "currency_agent"


class _DirectAgent:
    """Minimal ``SupportsAgentRun`` stub so A2AExecutor can run without a model.

    Implements only what A2AExecutor calls: ``create_session`` and ``run``
    returning an object with ``.messages``. Keeps Agent Framework's A2A
    conversion in the path while removing the model and its credentials.
    """

    name = AGENT_NAME

    def create_session(self, session_id: str | None = None, **kwargs):
        return None

    async def run(self, query, session=None, stream: bool = False, **kwargs):
        from agent_framework import AgentResponse, Message

        text = await deterministic_reply(str(query))
        return AgentResponse(messages=[Message(role="assistant", contents=[text])])


def _foundry_agent():
    """Native brain: a Foundry-hosted model behind an Agent Framework Agent."""
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential

    provider = rate_provider()

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

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )
    return Agent(
        client=client,
        name=AGENT_NAME,
        description="Answers currency conversion questions with structured quotes.",
        instructions=INSTRUCTION,
        tools=[get_exchange_rate],
        default_options={"store": False},
    )


def build() -> tuple:
    from agent_framework_a2a import A2AExecutor

    agent = _foundry_agent() if model_mode() == "llm" else _DirectAgent()
    card = build_agent_card(name=AGENT_NAME, url=public_url(DEFAULT_PORT))
    return build_app(A2AExecutor(agent), card), card


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
