"""Transport-independent A2A wire helpers shared by every client stack.

Named ``protocol`` rather than ``a2a`` so it cannot shadow the installed
``a2a-sdk`` package, which one of the three client stacks imports.
"""

from protocol.quotes import build_prompt, extract_json_objects, parse_quotes

__all__ = ["build_prompt", "extract_json_objects", "parse_quotes"]
