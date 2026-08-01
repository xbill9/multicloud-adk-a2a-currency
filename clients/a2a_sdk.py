"""Client stack 1: the vendor-neutral ``a2a-sdk`` 1.x reference client.

This is the stack the Bedrock coordinator uses. It is the only one of the
three that is not tied to an agent framework, so it doubles as the control
row of the interop matrix: a failure here is a server-side wire problem, not
a framework quirk.
"""

import httpx

from clients.base import A2AQuoteClient


def _task_texts(task) -> list[str]:
    """Pull the agent's reply out of a completed Task.

    Interop finding, matrix cells (a2a-sdk -> ADK) and (a2a-sdk -> Agent
    Framework): a Task carries the answer in a *different field* depending on
    who built the server. ADK's executor attaches it as an artifact; Agent
    Framework's A2AExecutor drives the task lifecycle and leaves the reply as
    a ROLE_AGENT message in history, with artifacts empty. Reading only
    ``artifacts`` -- the obvious implementation, and the one that works fine
    against Google -- silently returns an empty string against Microsoft.
    So read every carrier the spec allows.
    """
    from a2a.helpers import get_message_text, get_text_parts
    from a2a.types import Role

    texts: list[str] = []
    for artifact in task.artifacts:
        texts.append("\n".join(get_text_parts(artifact.parts)))
    if task.status.HasField("message"):
        texts.append(get_message_text(task.status.message))
    for message in task.history:
        if message.role == Role.ROLE_AGENT:
            texts.append(get_message_text(message))
    return [text for text in texts if text]


class A2ASdkClient(A2AQuoteClient):
    stack = "a2a-sdk"

    async def _send(self, prompt: str) -> str:
        from a2a.client import A2ACardResolver, ClientConfig, create_client
        from a2a.helpers import get_message_text, get_text_parts, new_text_message
        from a2a.types import Role, SendMessageRequest

        # local_address pins the socket to IPv4; IPv6 to some hosts hangs in
        # some sandboxes (same workaround as FrankfurterRateProvider).
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        async with self._http_client(transport=transport) as httpx_client:
            resolver = A2ACardResolver(httpx_client=httpx_client, base_url=self._endpoint)
            card = await resolver.get_agent_card()
            # Interop workaround, matrix cell (a2a-sdk -> ADK): ADK's to_a2a()
            # advertises the server's *bind* address (e.g. http://127.0.0.1:8080)
            # on the card, and this client routes transport by card URL, so a
            # remote agent is unreachable without rewriting the interfaces to
            # the endpoint we dialled. Kept deliberately unconditional -- it is
            # the finding, and it costs nothing against well-behaved cards.
            for interface in card.supported_interfaces:
                interface.url = self._endpoint
            client = await create_client(
                agent=card,
                client_config=ClientConfig(streaming=False, httpx_client=httpx_client),
            )
            try:
                message_request = SendMessageRequest(
                    message=new_text_message(prompt, role=Role.ROLE_USER)
                )
                texts: list[str] = []
                async for chunk in client.send_message(message_request):
                    if chunk.HasField("message"):
                        texts.append(get_message_text(chunk.message))
                    elif chunk.HasField("task"):
                        texts.extend(_task_texts(chunk.task))
                return "\n".join(text for text in texts if text)
            finally:
                await client.close()
