"""The three A2A client stacks, one per vendor, behind one interface.

Imports are deferred to keep a single stack usable without installing all
three vendors' SDKs -- the matrix reports a missing stack as an uninstalled
row rather than crashing the run.
"""

from clients.base import A2AQuoteClient

CLIENT_STACKS = ("a2a-sdk", "agent-framework", "google-adk")


def load_client(stack: str, endpoint: str, **kwargs) -> A2AQuoteClient:
    """Construct a client for ``stack``; raises ImportError if its SDK is absent."""
    if stack == "a2a-sdk":
        from clients.a2a_sdk import A2ASdkClient

        return A2ASdkClient(endpoint, **kwargs)
    if stack == "agent-framework":
        from clients.agent_framework import AgentFrameworkClient

        return AgentFrameworkClient(endpoint, **kwargs)
    if stack == "google-adk":
        from clients.adk import AdkClient

        return AdkClient(endpoint, **kwargs)
    raise ValueError(f"unknown client stack: {stack!r} (expected one of {CLIENT_STACKS})")


__all__ = ["CLIENT_STACKS", "A2AQuoteClient", "load_client"]
