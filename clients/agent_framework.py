"""Client stack 2: Microsoft Agent Framework's ``A2AAgent``.

The stack the Foundry coordinator uses, and the most ergonomic of the three:
construct an agent from a URL, call ``run``, read ``.text``. Card resolution
and transport selection are entirely internal, which is convenient but leaves
no seam to patch when a server advertises a bad card -- see the a2a-sdk client
for the workaround this stack cannot express.
"""

from clients.base import A2AQuoteClient


class AgentFrameworkClient(A2AQuoteClient):
    stack = "agent-framework"

    def __init__(self, endpoint: str, *, agent_name: str = "currency_agent", **kwargs) -> None:
        super().__init__(endpoint, **kwargs)
        self._agent_name = agent_name

    async def _send(self, prompt: str) -> str:
        from agent_framework_a2a import A2AAgent

        agent = A2AAgent(name=self._agent_name, url=self._endpoint)
        response = await agent.run(prompt)
        return response.text or ""
