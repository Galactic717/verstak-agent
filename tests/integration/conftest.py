import asyncio
import sys
from collections.abc import Callable


def pytest_asyncio_loop_factories() -> dict[str, Callable[[], asyncio.AbstractEventLoop]] | None:
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None
