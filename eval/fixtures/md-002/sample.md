# Python Async Programming Guide

A comprehensive reference for writing concurrent Python applications using asyncio,
structured concurrency patterns, and production-ready async I/O.

## The Event Loop

The event loop is the core scheduler in asyncio. It runs coroutines, schedules
callbacks, and handles I/O events using the operating system's selector API (epoll on
Linux, kqueue on macOS). Only one coroutine runs at a time inside the loop; concurrency
comes from cooperative yielding, not OS-level threads.

To obtain the running loop from within a coroutine, call `asyncio.get_running_loop()`.
From outside any coroutine, use `asyncio.get_event_loop()` in Python 3.9 or earlier,
or `asyncio.new_event_loop()` when building servers. Never block the event loop with
CPU-bound work — use `loop.run_in_executor()` to offload to a thread or process pool.

## Coroutines and Await

A coroutine is a function defined with `async def`. Calling it returns a coroutine
object; the object does not execute until it is awaited or scheduled. The `await`
expression suspends the current coroutine and transfers control back to the event loop,
which may run other coroutines before returning with the awaited result.

Coroutines are cheap — creating thousands is fine. They are garbage-collected when no
reference holds them and they have not been scheduled. Forgetting to `await` a coroutine
is a common bug: the coroutine object is silently discarded and nothing runs. Enable
`PYTHONASYNCIODEBUG=1` to surface these warnings during development.

## Tasks and Scheduling

`asyncio.create_task()` wraps a coroutine in a Task and schedules it on the running
loop immediately. Unlike a bare coroutine, a Task runs concurrently with the caller —
the caller does not need to await the Task for it to make progress.

Tasks hold a strong reference inside the event loop. If you fire-and-forget a Task
without storing a reference, the event loop keeps it alive until it completes or the
loop closes. To avoid silent failures, always attach a done callback with
`task.add_done_callback()` that logs or re-raises exceptions.

## Gathering Multiple Coroutines

`asyncio.gather(*coros)` schedules all coroutines concurrently and returns their results
in order. If any coroutine raises, gather raises the first exception by default; set
`return_exceptions=True` to collect results and exceptions together.

Use `gather` when you have a fixed, known set of coroutines to run in parallel and want
a single await point. For dynamic fan-out — where the number of tasks is determined at
runtime — prefer `asyncio.TaskGroup` (Python 3.11+), which provides structured
cancellation: if any child task raises, all siblings are cancelled automatically.

## Semaphores for Concurrency Control

An `asyncio.Semaphore` limits the number of coroutines that can proceed past a given
point simultaneously. This is essential when calling rate-limited APIs or opening a
bounded number of database connections.

```python
sem = asyncio.Semaphore(10)

async def fetch(url: str) -> bytes:
    async with sem:
        async with session.get(url) as resp:
            return await resp.read()
```

The semaphore is released automatically when the `async with` block exits, even on
exception. Never share a Semaphore across multiple event loops.

## Timeouts

`asyncio.wait_for(coro, timeout)` cancels the wrapped coroutine if it does not complete
within `timeout` seconds, raising `asyncio.TimeoutError`. In Python 3.11+, prefer
`asyncio.timeout()` as a context manager, which can be rescheduled and inspected.

Cancellation in asyncio is cooperative: the `CancelledError` is injected at the next
`await` point. If your coroutine catches `BaseException` and does cleanup, it must
re-raise `CancelledError` to allow proper propagation. Swallowing `CancelledError`
creates zombies that hold resources and block shutdown.

## Context Variables

`contextvars.ContextVar` provides coroutine-local storage analogous to thread-local
storage for threads. Each Task inherits a copy of the context from its creator, so
mutations in a child Task do not affect the parent's context.

Common uses: request-scoped logging context (trace IDs, user IDs), locale settings,
and transaction handles. Always provide a `default` or call `.get()` with a fallback to
avoid `LookupError` in code paths that run outside of a request.

## Async Iterators and Generators

`async for` iterates over an `AsyncIterable` — any object implementing `__aiter__` and
`__anext__`. Async generators (`async def` with `yield`) implement this protocol
automatically and are the idiomatic way to stream data: read a database cursor page by
page, yield chunks from a file, or paginate an API.

Async generators must be explicitly closed (`aclose()`) when the consumer exits early,
or use `contextlib.asynccontextmanager` to ensure cleanup. Forgetting this leaves open
connections and unreleased locks.

## Testing Async Code

Use `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml` to avoid marking
every async test with `@pytest.mark.asyncio`. Fixtures declared `async def` are
automatically awaited.

Mock async callables with `unittest.mock.AsyncMock` — it returns a coroutine on call
and tracks `await` counts via `.assert_awaited_once()`. For event-loop-sensitive code,
avoid sharing loops between tests: each test should run in a fresh loop (pytest-asyncio
handles this by default).

## Queue-Based Producer-Consumer Patterns

`asyncio.Queue` decouples producers from consumers without threads. Producers call
`await queue.put(item)` and consumers call `await queue.get()` followed by
`queue.task_done()`. Call `await queue.join()` to block until all items are processed.

Set `maxsize` to apply backpressure: producers block when the queue is full, preventing
unbounded memory growth during consumer lag. This pattern is the async equivalent of
thread-pool work queues and is the foundation for pipeline stages in ingestion systems.

## Structured Concurrency with TaskGroup

`asyncio.TaskGroup` (Python 3.11+) is the preferred way to run concurrent tasks when
all tasks belong to a logical unit of work. Tasks are spawned with `tg.create_task()`.
When the `async with` block exits — normally or via exception — the group waits for all
tasks and cancels any that are still running.

This eliminates the "forgotten task" problem: every task spawned inside a TaskGroup is
accounted for. Exceptions from any child propagate to the parent scope wrapped in an
`ExceptionGroup`, enabling fine-grained handling with `except*` syntax.

## Signal Handling and Graceful Shutdown

Register signal handlers with `loop.add_signal_handler(signal.SIGTERM, handler)` rather
than the stdlib `signal.signal()`, which is not async-safe. On shutdown, cancel all
running Tasks, await them, and call `loop.run_until_complete(loop.shutdown_default_executor())`
to drain the default thread-pool executor.

For web servers, wrap startup and shutdown in an `asyncio.TaskGroup` or use a framework
lifecycle hook. Never call `loop.close()` before all Tasks have completed — dangling
Tasks will log warnings and may corrupt shared state.
