"""Run blocking foreground terminal work without abandoning the Textual application."""

from __future__ import annotations

import signal
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol, TypeVar, cast


class Suspendable(Protocol):
    """The part of Textual's application interface needed for foreground work."""

    def suspend(self) -> AbstractContextManager[None]: ...


Result = TypeVar("Result")


def run_suspended(app: Suspendable, callback: Callable[[], Result]) -> Result:
    """Run ``callback`` with a restored terminal and synchronous Ctrl-C handling.

    Textual's application is normally driven by ``asyncio.run``, whose first SIGINT cancels the
    main task instead of interrupting a blocking input call. Temporarily restoring Python's
    synchronous handler makes one Ctrl-C raise ``KeyboardInterrupt`` at that call.

    Textual 8.2's suspend context resumes application mode only after its yield completes normally.
    Capture every callback exception inside the context, then propagate it after terminal mode and
    the prior signal handler have both been restored.
    """

    result: Result | None = None
    failure: BaseException | None = None
    with app.suspend():
        previous_handler = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            result = callback()
        except BaseException as exc:
            failure = exc
        finally:
            signal.signal(signal.SIGINT, previous_handler)
    if failure is not None:
        raise failure
    return cast(Result, result)
