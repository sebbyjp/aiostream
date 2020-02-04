"""Utilities for testing stream operators."""

import time
import asyncio
from unittest.mock import Mock, ANY
from contextlib import contextmanager
from selectors import DefaultSelector

import pytest

from . import compat
from .core import StreamEmpty, operator, streamcontext

__all__ = ['add_resource', 'assert_run', 'event_loop']


@pytest.fixture
def add_resource(event_loop):

    @operator(pipable=True)
    async def add_resource(source, cleanup_time):
        """Simulate an open resource in a stream operator."""
        try:
            event_loop.open_resources += 1
            event_loop.resources += 1
            async with streamcontext(source) as streamer:
                async for item in streamer:
                    yield item
        finally:
            try:
                await compat.sleep(cleanup_time)
            finally:
                event_loop.open_resources -= 1

    return add_resource


def compare_exceptions(exc1, exc2):
    """Compare two exceptions together."""
    return (
        exc1 == exc2
        or exc1.__class__ == exc2.__class__
        and exc1.args == exc2.args)


async def assert_aiter(source, values, exception=None):
    """Check the results of a stream using a streamcontext."""
    it = iter(values)
    exception_type = type(exception) if exception else ()
    try:
        async with streamcontext(source) as streamer:
            async for item in streamer:
                assert item == next(it)
    except exception_type as exc:
        assert compare_exceptions(exc, exception)
    else:
        assert exception is None


async def assert_await(source, values, exception=None):
    """Check the results of a stream using by awaiting it."""
    exception_type = type(exception) if exception else ()
    try:
        result = await source
    except StreamEmpty:
        assert values == []
        assert exception is None
    except exception_type as exc:
        assert compare_exceptions(exc, exception)
    else:
        assert result == values[-1]
        assert exception is None


@pytest.fixture(
    params=[assert_aiter, assert_await],
    ids=['aiter', 'await'])
def assert_run(request, event_loop):
    """Parametrized fixture returning a stream runner."""
    return request.param


@pytest.fixture
def event_loop(anyio_backend, monkeypatch, autojump_clock):
    """Fixture providing a test event loop.

    The event loop simulate and records the sleep operation,
    available as event_loop.steps

    It also tracks simulated resources and make sure they are
    all released before the loop is closed.
    """
    backend_options = {}

    # Asyncio: disable uvloop and use time tracking

    if anyio_backend == "asyncio":
        loop = TimeTrackingTestLoop()
        asyncio.set_event_loop(loop)
        backend_options["use_uvloop"] = False

    # Trio: use a mock clock

    if anyio_backend == "trio":
        from trio.testing import MockClock
        loop = NotAnActualLoop()
        backend_options["clock"] = MockClock(rate=10, autojump_threshold=0.001)

    # Curio: mock `curio.kernel.time`

    if anyio_backend == "curio":
        loop = NotAnActualLoop()
        selector = DefaultSelector()
        original = selector.select
        monkeypatch.setattr(selector, "select", lambda x: original(0))
        backend_options["selector"] = selector
        monkeypatch.setattr("curio.kernel.time", loop)

    # Monkey-patch backend options in anyio

    def patched_run(*args, **kwargs):
        kwargs = {"backend_options": backend_options, **kwargs}
        return compat.anyio.run(*args, **kwargs)

    monkeypatch.setattr("anyio.pytest_plugin.run", patched_run)

    # Yield the loop

    with loop.assert_cleanup():
        yield loop
    loop.close()


# Loop mock classes

class ResourceMixin:

    def clear(self):
        self.steps = []
        self.open_resources = 0
        self.resources = 0
        self.busy_count = 0

    @contextmanager
    def assert_cleanup(self):
        self.clear()
        yield self
        assert self.open_resources == 0
        self.clear()


class NotAnActualLoop(ResourceMixin):

    def monotonic(self):
        return time.monotonic() * 1000

    def clear(self):
        super().clear()
        self.steps = ANY

    def close(self):
        pass


class TimeTrackingTestLoop(asyncio.BaseEventLoop, ResourceMixin):

    stuck_threshold = 100

    def __init__(self):
        super().__init__()
        self._time = 0
        self._timers = []
        self._selector = Mock()
        self.clear()

    # Loop internals

    def _run_once(self):
        super()._run_once()
        # Update internals
        self.busy_count += 1
        self._timers = sorted(
            when for when in self._timers if when > self.time())
        # Time advance
        if self.time_to_go:
            when = self._timers.pop(0)
            step = when - self.time()
            self.steps.append(step)
            self.advance_time(step)
            self.busy_count = 0

    def _process_events(self, event_list):
        return  # pragma: no cover

    def _write_to_self(self):
        return  # pragma: no cover

    # Time management

    def time(self):
        return self._time

    def advance_time(self, advance):
        if advance:
            self._time += advance

    def call_at(self, when, callback, *args, **kwargs):
        self._timers.append(when)
        return super().call_at(when, callback, *args, **kwargs)

    @property
    def stuck(self):
        return self.busy_count > self.stuck_threshold

    @property
    def time_to_go(self):
        return self._timers and (self.stuck or not self._ready)
