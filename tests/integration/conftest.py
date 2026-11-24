import asyncio
import sys
from collections.abc import Callable

if sys.platform == "win32":

    def pytest_asyncio_loop_factories() -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
        return {"selector": asyncio.SelectorEventLoop}
