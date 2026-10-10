"""Share one in-flight wait between identical concurrent calls.

Models sometimes fire the same wait several times in parallel (the same session_wait six to
fourteen times over). Each copy would hold a thread and a relay request for up to 45 s and
then return the same output; instead they all await one task and get the same answer.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

_inflight: dict[Hashable, asyncio.Task] = {}


def freeze(value: Any) -> Hashable:
    """A hashable stand-in for tool arguments (lists and dicts included)."""
    if isinstance(value, dict):
        return tuple(sorted((str(k), freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(freeze(v) for v in value)
    return value


async def shared(key: Hashable, start: Callable[[], Awaitable[Any]]) -> Any:
    """Await the running task for `key`, starting it with `start()` when there is none.
    A caller that gives up (is cancelled) does not cancel the wait for the others."""
    task = _inflight.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(start())
        _inflight[key] = task

        def forget(done: asyncio.Task, k: Hashable = key) -> None:
            if _inflight.get(k) is done:
                del _inflight[k]

        task.add_done_callback(forget)
    result = await asyncio.shield(task)
    return copy.deepcopy(result) if isinstance(result, (dict, list)) else result


def inflight() -> int:
    return len(_inflight)
