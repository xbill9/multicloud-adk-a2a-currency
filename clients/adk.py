"""Client stack 3: Google ADK's ``RemoteA2aAgent``.

The heaviest of the three by a wide margin. ``RemoteA2aAgent`` is a
``BaseAgent`` intended to sit inside an agent tree as a sub-agent, not to be
called directly, so using it as a plain client means standing up a Runner, a
session service, and a session for a single request-response. That cost is
itself a matrix finding: the ADK stack assumes A2A is something an agent does,
not something a program does.
"""

from clients.base import A2AQuoteClient

_APP_NAME = "currency-matrix"
_USER_ID = "matrix-runner"


class AdkClient(A2AQuoteClient):
    stack = "google-adk"

    def __init__(self, endpoint: str, *, agent_name: str = "remote_currency_agent", **kwargs
                 ) -> None:
        super().__init__(endpoint, **kwargs)
        self._agent_name = agent_name

    @property
    def _card_url(self) -> str:
        return f"{self._endpoint}/.well-known/agent-card.json"

    async def _send(self, prompt: str) -> str:
        from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        async with self._http_client() as httpx_client:
            remote = RemoteA2aAgent(
                name=self._agent_name,
                agent_card=self._card_url,
                httpx_client=httpx_client,
            )
            session_service = InMemorySessionService()
            runner = Runner(
                agent=remote,
                app_name=_APP_NAME,
                session_service=session_service,
            )
            session = await session_service.create_session(app_name=_APP_NAME, user_id=_USER_ID)

            texts: list[str] = []
            try:
                async for event in runner.run_async(
                    user_id=_USER_ID,
                    session_id=session.id,
                    new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
                ):
                    if event.content and event.content.parts:
                        texts.extend(part.text for part in event.content.parts if part.text)
            finally:
                close = getattr(runner, "close", None)
                if close is not None:
                    await close()
            return "\n".join(texts)
