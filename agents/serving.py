"""a2a-sdk 1.x serving scaffolding shared by the AWS and Azure agents.

Both sit on the reference Starlette routes; they differ in the executor that
turns a request into a reply. Google's agent does not use this module at all
-- ADK's ``to_a2a()`` builds its own app, which is precisely the difference
the matrix is looking for.
"""

from collections.abc import Awaitable, Callable

from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Role
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.common import DESCRIPTION, INSTRUCTION, deterministic_reply, model_mode

Responder = Callable[[str], Awaitable[str]]


class CallbackExecutor(AgentExecutor):
    """Adapt a plain ``async (prompt) -> reply`` function to the A2A executor API.

    Enqueues a single Message, the immediate-response workflow the executor
    contract documents, which is all a currency quote needs.
    """

    def __init__(self, responder: Responder) -> None:
        self._responder = responder

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        prompt = context.get_user_input()
        reply = await self._responder(prompt)
        await event_queue.enqueue_event(new_text_message(reply, role=Role.ROLE_AGENT))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            new_text_message("currency quotes are not cancellable", role=Role.ROLE_AGENT)
        )


def direct_executor() -> CallbackExecutor:
    """The credential-free brain: deterministic quotes from the rate provider."""
    return CallbackExecutor(deterministic_reply)


def build_agent_card(*, name: str, url: str, version: str = "0.1.0") -> AgentCard:
    return AgentCard(
        name=name,
        description=DESCRIPTION,
        version=version,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        # The advertised URL is configuration, never the bind address.
        supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC")],
        skills=[
            AgentSkill(
                id="currency_conversion",
                name="currency conversion",
                description=INSTRUCTION,
                tags=["currency", "exchange-rate", "finance"],
            )
        ],
    )


def build_app(executor: AgentExecutor, card: AgentCard) -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    async def health(request):
        # `brain` is reported by the agent because only the agent knows it.
        # The matrix used to print CURRENCY_MODEL_MODE from its *own* process,
        # which is a different container once deployed -- a run against three
        # `llm` agents duly printed brain=direct.
        return JSONResponse({"status": "ok", "agent": card.name, "brain": model_mode()})

    async def ping(request):
        """AgentCore Runtime's health contract.

        Required verbatim: ``status`` must be ``Healthy`` or ``HealthyBusy``.
        Deliberately omits ``time_of_last_update`` -- a timestamp that advances
        on every ping reads as a continuous status change, which stops the idle
        session timeout from ever firing and leaks sessions until MaxLifetime.
        Omitting it lets the platform track changes itself.
        """
        return JSONResponse({"status": "Healthy"})

    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
            Route("/health", health, methods=["GET"]),
            Route("/ping", ping, methods=["GET"]),
        ]
    )
