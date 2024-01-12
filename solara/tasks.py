import abc
import asyncio
import dataclasses
import inspect
import logging
import threading
from enum import Enum
from typing import (
    Any,
    Callable,
    Coroutine,
    Generic,
    Optional,
    TypeVar,
    Union,
    cast,
    overload,
)

import typing_extensions

import solara
import solara.util
from solara.toestand import Singleton

from .toestand import Ref as ref

R = TypeVar("R")
T = TypeVar("T")
P = typing_extensions.ParamSpec("P")

logger = logging.getLogger("solara.task")

try:
    threading.Thread(target=lambda: None).start()
    has_threads = True
except RuntimeError:
    has_threads = False
has_threads


class TaskState(Enum):
    NOTCALLED = 1
    STARTING = 2
    WAITING = 3
    RUNNING = 4
    ERROR = 5
    FINISHED = 6
    CANCELLED = 7


@dataclasses.dataclass(frozen=True)
class TaskResult(Generic[T]):
    value: Optional[T] = None
    latest: Optional[T] = None
    exception: Optional[Exception] = None
    _state: TaskState = TaskState.NOTCALLED
    progress: Optional[float] = None

    @property
    def not_called(self):
        return self._state == TaskState.NOTCALLED

    @property
    def pending(self):
        return self._state in (TaskState.STARTING, TaskState.WAITING, TaskState.RUNNING)

    @property
    def finished(self):
        return self._state == TaskState.FINISHED

    @property
    def cancelled(self):
        return self._state == TaskState.CANCELLED

    @property
    def error(self):
        return self._state == TaskState.ERROR


class Task(Generic[P, R], abc.ABC):
    def __init__(self):
        self._result = solara.reactive(
            TaskResult[R](
                value=None,
                _state=TaskState.NOTCALLED,
            )
        )
        self._last_value: Optional[R] = None
        self._last_progress: Optional[float] = None
        self._latest = ref(self._result.fields.latest)

    @property
    def result(self) -> TaskResult[R]:
        return self._result.value

    @property
    def latest(self) -> Optional[R]:
        return self._latest.value

    @property
    def value(self) -> Optional[R]:
        return self._result.value.value

    # @property
    # def _state(self) -> solara.ResultState:
    #     return self._result.value.state if self._result.value is not None else TaskState.INITIAL

    @property
    def error(self) -> Optional[Exception]:
        return self._result.value.error

    @property
    def finished(self) -> bool:
        return self._result.value.finished

    @property
    def cancelled(self) -> bool:
        return self._result.value.cancelled

    @property
    def pending(self) -> bool:
        return self._result.value.pending

    @property
    def not_called(self) -> bool:
        return self._result.value.not_called

    @property
    def progress(self) -> Optional[float]:
        return self._result.value.progress

    @progress.setter
    def progress(self, value: Optional[float]) -> None:
        self._last_progress = value
        self._result.value = dataclasses.replace(self._result.value, progress=self._last_progress)

    @property
    def exception(self) -> Optional[Exception]:
        return self._result.value.exception

    @abc.abstractmethod
    def retry(self) -> None:
        ...

    @abc.abstractmethod
    def cancel(self) -> None:
        ...

    @abc.abstractmethod
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None:
        ...


class _CancelledErrorInOurTask(BaseException):
    pass


class TaskAsyncio(Task[P, R]):
    current_task: Optional[asyncio.Task] = None
    current_future: Optional[asyncio.Future] = None
    _cancel: Optional[Callable[[], None]] = None
    _retry: Optional[Callable[[], None]] = None

    def __init__(self, run_in_thread: bool, function: Callable[P, Coroutine[Any, Any, R]]):
        self.run_in_thread = run_in_thread
        self.function = function
        super().__init__()

    def cancel(self) -> None:
        if self._cancel:
            self._cancel()
        else:
            raise RuntimeError("Cannot cancel task, never started")

    def retry(self):
        if self._retry:
            self._retry()
        else:
            raise RuntimeError("Cannot retry task, never started")

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None:
        self.current_future = future = asyncio.Future[R]()
        self._last_progress = None
        current_task: asyncio.Task[None]
        if self.current_task:
            self.current_task.cancel()

        def retry():
            self(*args, **kwargs)

        def cancel():
            event_loop = current_task.get_loop()
            # cancel after cancel is a no-op
            self._cancel = lambda: None
            if asyncio.current_task() == current_task:
                if event_loop == asyncio.get_event_loop():
                    # we got called in our own task and event loop
                    raise _CancelledErrorInOurTask()
                else:
                    current_task.cancel()
                    self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.CANCELLED)
            else:
                current_task.cancel()
                self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.CANCELLED)

        self._cancel = cancel
        self._retry = retry
        call_event_loop = asyncio.get_event_loop()
        if self.run_in_thread:
            thread_event_loop = asyncio.new_event_loop()
            self.current_task = current_task = thread_event_loop.create_task(self._async_run(call_event_loop, future, args, kwargs))

            def runs_in_thread():
                try:
                    thread_event_loop.run_until_complete(current_task)
                except asyncio.CancelledError as e:
                    call_event_loop.call_soon_threadsafe(future.set_exception, e)
                except Exception as e:
                    logger.exception("error running in thread")
                    call_event_loop.call_soon_threadsafe(future.set_exception, e)
                    raise

            self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.STARTING)
            thread = threading.Thread(target=runs_in_thread)
            thread.start()
        else:
            self.current_task = current_task = asyncio.create_task(self._async_run(call_event_loop, future, args, kwargs))
            self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.STARTING)

    async def _async_run(self, call_event_loop: asyncio.AbstractEventLoop, future: asyncio.Future, args, kwargs) -> None:
        task_for_this_call = asyncio.current_task()
        assert task_for_this_call is not None

        def still_active():
            assert task_for_this_call is not None
            return (self.current_task == task_for_this_call) and not task_for_this_call.cancelled()

        assert self.current_task is task_for_this_call

        if still_active():
            self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.STARTING)

        async def runner():
            try:
                if still_active():
                    self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.RUNNING)
                self._last_value = value = await self.function(*args, **kwargs)
                if still_active() and not task_for_this_call.cancelled():
                    self._result.value = TaskResult[R](value=value, latest=value, _state=TaskState.FINISHED, progress=self._last_progress)
                logger.info("setting result to %r", value)
                call_event_loop.call_soon_threadsafe(future.set_result, value)
            except Exception as e:
                if still_active():
                    logger.exception(e)
                    self._result.value = TaskResult[R](latest=self._last_value, exception=e, _state=TaskState.ERROR)
                call_event_loop.call_soon_threadsafe(future.set_exception, e)
            # Although this seems like an easy way to handle cancellation, an early cancelled task will never execute
            # so this code will never execute, so we need to handle this in the cancel function in __call__
            # except asyncio.CancelledError as e:
            #    if still_active():
            #        self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.CANCELLED)
            #    call_event_loop.call_soon_threadsafe(future.set_exception, e)
            # But... if we call cancel in our own task, we still need to do it from this place
            except _CancelledErrorInOurTask as e:
                try:
                    # maybe there is a different way to get a full stack trace?
                    raise asyncio.CancelledError() from e
                except asyncio.CancelledError as e:
                    if still_active():
                        self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.CANCELLED)
                    call_event_loop.call_soon_threadsafe(future.set_exception, e)

        await runner()


class TaskThreaded(Task[P, R]):
    _current_cancel_event: Optional[threading.Event] = None
    current_thread: Optional[threading.Thread] = None
    running_thread: Optional[threading.Thread] = None
    _last_finished_event: Optional[threading.Event] = None
    _cancel: Optional[Callable[[], None]] = None
    _retry: Optional[Callable[[], None]] = None

    def __init__(self, function: Callable[P, R]):
        super().__init__()
        self.__qualname__ = function.__qualname__
        self.function = function
        self.lock = threading.Lock()

    def cancel(self) -> None:
        if self._cancel:
            self._cancel()
        else:
            raise RuntimeError("Cannot cancel task, never started")

    def retry(self):
        if self._retry:
            self._retry()
        else:
            raise RuntimeError("Cannot retry task, never started")

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None:
        self._last_finished_event = _last_finished_event = threading.Event()
        self._current_cancel_event = cancel_event = threading.Event()
        self._last_progress = None

        def retry():
            self(*args, **kwargs)

        def cancel():
            cancel_event.set()
            if threading.current_thread() == current_thread:
                raise solara.util.CancelledError()
            self._current_cancel_event = None

        self._retry = retry
        self._cancel = cancel

        self.current_thread = current_thread = threading.Thread(target=lambda: self._run(_last_finished_event, cancel_event, args, kwargs), daemon=True)
        self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.STARTING)
        current_thread.start()

    def _run(self, _last_finished_event, cancel_event, args, kwargs) -> None:
        def am_i_the_last_called_thread():
            return self.running_thread == threading.current_thread()

        def runner():
            intrusive_cancel = True
            wait_for_thread = None
            with self.lock:
                # if there is a current thread already, we'll need
                # to wait for it. copy the ref, and set ourselves
                # as the current one
                if self.running_thread:
                    wait_for_thread = self.running_thread
                self.running_thread = threading.current_thread()
            if wait_for_thread is not None:
                if am_i_the_last_called_thread():
                    self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.WAITING)
                # don't start before the previous is stopped
                try:
                    wait_for_thread.join()
                except:  # noqa
                    pass
                if threading.current_thread() != self.running_thread:
                    # in case a new thread was started that also was waiting for the previous
                    # thread to st stop, we can finish this
                    return
            # we previously set current to None, but if we do not do that, we can still render the old value
            # while we can still show a loading indicator using the .state
            # result.current = None
            if am_i_the_last_called_thread():
                self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.RUNNING)

            callback = self.function
            try:
                guard = solara.util.cancel_guard(cancel_event) if intrusive_cancel else solara.util.nullcontext()
                try:
                    # we only use the cancel_guard context manager around
                    # the function calls to f. We don't want to guard around
                    # a call to react, since that might slow down rendering
                    # during rendering
                    with guard:
                        if am_i_the_last_called_thread():
                            value = callback(*args, **kwargs)
                    if inspect.isgenerator(value):
                        generator = value
                        self._last_value = None
                        while True:
                            try:
                                with guard:
                                    self._last_value = value = next(generator)
                                    if am_i_the_last_called_thread():
                                        self._result.value = TaskResult[R](latest=value, value=value, _state=TaskState.RUNNING, progress=self._last_progress)
                            except StopIteration:
                                break
                        if am_i_the_last_called_thread():
                            self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.FINISHED, progress=self._last_progress)
                    else:
                        self._last_value = value
                        if am_i_the_last_called_thread():
                            self._result.value = TaskResult[R](latest=value, value=value, _state=TaskState.FINISHED, progress=self._last_progress)
                except Exception as e:
                    if am_i_the_last_called_thread():
                        logger.exception(e)
                        self._last_value = None
                        self._result.value = TaskResult[R](latest=self._last_value, exception=e, _state=TaskState.ERROR)
                    return
                except solara.util.CancelledError:
                    pass
                    # this means this thread is cancelled not be request, but because
                    # a new thread is running, we can ignore this
            finally:
                if am_i_the_last_called_thread():
                    self.running_thread = None
                    logger.info("thread done!")
                    if cancel_event.is_set():
                        self._result.value = TaskResult[R](latest=self._last_value, _state=TaskState.CANCELLED)
                _last_finished_event.set()

        runner()


# TODO: Not sure if we want to use this, or have all local variables in Task subclasses be reactive vars
class Proxy:
    def __init__(self, factory):
        self._instance = Singleton(factory)

    def __getattr__(self, name):
        return getattr(self._instance.value, name)

    def __setattr__(self, name, value):
        if name == "_instance":
            super().__setattr__(name, value)
        else:
            setattr(self._instance.value, name, value)

    def __call__(self, *args, **kwargs):
        return self._instance.value(*args, **kwargs)


@overload
def task(
    f: None = None,
    *,
    prefer_threaded: bool = ...,
) -> Callable[[Callable[P, R]], Task[P, R]]:
    ...


@overload
def task(
    f: Callable[P, Union[Coroutine[Any, Any, R], R]],
    *,
    prefer_threaded: bool = ...,
) -> Task[P, R]:
    ...


def task(
    f: Union[None, Callable[P, Union[Coroutine[Any, Any, R], R]]] = None,
    *,
    prefer_threaded: bool = True,
) -> Union[Callable[[Callable[P, R]], Task[P, R]], Task[P, R]]:
    """Decorator to turn a function or coroutine function into a task.

    Lets you run code in the background, with the UI available to the user. This is useful for long running tasks, like downloading data or processing data.

    The task decorator turns a function or coroutine function (`async def foo(...)` - here foo is called a coroutine function) into a task object.
    A task is a callable that will run the function in a separate thread for normal functions
    or an asyncio task for a coroutine function. Note that on platforms where threads are supported,
    asyncio tasks will still be executed in threads (unless the
    `prefer_thread=False` argument is passed). Because a coroutine function might still call long running blocking code,
    running the asyncio task in a thread will still result in a responsive UI when executed in a separate thread.

    The task object will execute the function only once per virtual kernel and will only store one result per virtual kernel.
    When called multiple times, the previously started run will be cancelled.

    The return value of the function is available as the `.value` reactive property on the task object, meaning that if a
    component accesses it, the component will automatically re-run when the value changes, like a [reactive variable](/api/reactive).

    ## Task object

    The task object has the following attributes/values which are all reactive:

    * `.value`: Contains the return value of the function (Only valid if `.finished` is true, else None).
    * `.exception`: The exception raised by the function, if any (Only valid if `.error` is true, else None).
    * `.latest` The last return value of the function, useful for showing out of date data while the task is running.
    * `.progress` A readable and writable reactive property which can be used for communicating progress to the user.

    The state of the task can be queried with the following attributes, which are all reactive:

    * `.not_called`: True if the task has not been called yet.
    * `.pending`: True if the task is asked to run, but did not finish yet, did not error and did not get cancelled.
        When true, often a loading or busy indicator is shown to the user.
    * `.finished`: True if the task has finished running. The result is available in the `.value` attribute as
        well as the `.latest` attribute.
    * `.cancelled`: True if the task was cancelled (by calling `.cancel()`).
    * `.error`: True if the function has raised an exception.

    The following methods are available:

    * `(*args, **kwargs)` : Call the task with the given arguments and keyword arguments. The task will only run once per virtual kernel.
    * `.cancel()`: Cancels the task.

    ## State diagram

    The following state diagram shows the possible states of a task and how
    each state transitions to another state.

    ```mermaid
    stateDiagram-v2
        not_called --> pending:  task()
        pending --> finished:
        pending --> error: exception
        pending --> pending:  task()
        pending --> cancelled: task.cancel()
        finished --> pending: task()
        error --> pending: task()
        cancelled --> pending: task()
    ```

    Note that calling the task (as indicated by `task()`) can be done from any state.


    ## Example

    ### Async task


    ```solara
    import asyncio
    import solara
    from solara.lab import task

    @task
    async def fetch_data():
        await asyncio.sleep(2)
        return "The answer is 42"

    @solara.component
    def Page():
        solara.Button("Fetch data", on_click=fetch_data)
        solara.ProgressLinear(fetch_data.pending)

        if fetch_data.finished:
            solara.Text(fetch_data.value)
        elif fetch_data.not_called:
            solara.Text("Click the button to fetch data")
        # Optional state check
        # elif fetch_data.cancelled:
        #     solara.Text("Cancelled the fetch")
        # elif fetch_data.error:
        #    solara.Error(str(fetch_data.exception))

    ```

    ### Threaded task

    ```solara
    import time
    import solara
    from solara.lab import task

    @task
    def fetch_data():
        time.sleep(2)
        return "The answer is 42"


    @solara.component
    def Page():
        solara.Button("Fetch data", on_click=fetch_data)
        solara.ProgressLinear(fetch_data.pending)

        if fetch_data.finished:
            solara.Text(fetch_data.value)
        elif fetch_data.not_called:
            solara.Text("Click the button to fetch data")
        # Optional state check
        # elif fetch_data.cancelled:
        #     solara.Text("Cancelled the fetch")
        # elif fetch_data.error:
        #    solara.Error(str(fetch_data.exception))
    ```

    Note that both examples are very similar. In the first example however, we wrap a coroutine function
    which can use `asyncio.sleep`. In the second example, we use a regular function, which uses `time.sleep`.
    If the coroutine function would use `time.sleep` in combination with `prefer_threaded=False`,
    the UI would be unresponsive for 2 seconds.


    ### Showing a progress bar


    Using the `.progress` attribute, you can show a progress bar to the user. This is useful for long running tasks
    but requires a bit more work.

    ```solara
    import time
    import solara
    from solara.lab import task


    @task
    def my_calculation():
        total = 0
        for i in range(10):
            my_calculation.progress = (i + 1) * 10.0
            time.sleep(0.4)
            total += i**2
        return total


    @solara.component
    def Page():
        solara.Button("Run calculation", on_click=my_calculation)
        solara.ProgressLinear(my_calculation.progress if my_calculation.pending else False)

        if my_calculation.finished:
            solara.Text(f"Calculation result: {my_calculation.value}")
        elif my_calculation.not_called:
            solara.Text("Click the button to fetch data")
        # Optional state check
        # elif my_calculation.cancelled:
        #     solara.Text("Cancelled the fetch")
        # elif my_calculation.error:
        #    solara.Error(str(my_calculation.exception))
    ```

    ## Arguments

    - `f`: Function to turn into task or None
    - `prefer_threaded` - bool: Will run coroutine functions as a task in a thread when threads are available.
        This ensures that even when a coroutine functions calls a blocking function the UI is still responsive.
        On platform where threads are not supported (like Pyodide / WASM / Emscripten / PyScript), a coroutine
        function will always run in the current event loop.

    ```

    """

    def wrapper(f: Union[None, Callable[P, Union[Coroutine[Any, Any, R], R]]]) -> Task[P, R]:
        def create_task():
            if inspect.iscoroutinefunction(f):
                return TaskAsyncio[P, R](prefer_threaded and has_threads, f)
            else:
                return TaskThreaded[P, R](cast(Callable[P, R], f))

        return cast(Task[P, R], Proxy(create_task))

    if f is None:
        return wrapper
    else:
        return wrapper(f)


@overload
def use_task(
    f: None = None,
    dependencies=[],
    *,
    raise_error=...,
    prefer_threaded=...,
) -> Callable[[Callable[[], R]], TaskResult[R]]:
    ...


@overload
def use_task(
    f: Callable[P, R],
    dependencies=[],
    *,
    raise_error=...,
    prefer_threaded=...,
) -> TaskResult[R]:
    ...


def use_task(
    f: Union[None, Callable[[], R]] = None,
    dependencies=[],
    *,
    raise_error=True,
    prefer_threaded=True,
) -> Union[Callable[[Callable[[], R]], TaskResult[R]], TaskResult[R]]:
    """A hook that runs a function or coroutine function as a task and return the result.

    Allows you to run code in the background, with the UI available to the user. This is useful for long running tasks,
    like downloading data or processing data.

    Unlike with the [`@task`](/api/task) decorator, the result is not globally shared, but only available to the component that called `use_task`.

    Note that unlike the [`@task`](/api/task) decorator, the task is invoked immediately, and the hook will return the only the result of the call
    (as a TaskResult object), instead of the task object.


    All the same attributes are available as with the task object returned by the [`@task`](/api/task) decorator, except for the methods (like `.cancel()`).

    ## Example

    ### Running in a thread

    ```solara
    import time
    import solara
    from solara.lab import use_task, TaskResult


    @solara.component
    def Page():
        number = solara.use_reactive(4)

        def square():
            time.sleep(1)
            return number.value**2

        result: TaskResult[bool] = use_task(square, dependencies=[number.value])

        solara.InputInt("Square", value=number, continuous_update=True)
        if result.finished:
            solara.Success(f"Square of {number} == {result.value}")
        solara.ProgressLinear(result.pending)
    ```

    ### Running in a asyncio task

    Note that the only difference is our function is now a coroutine function,
    and we use `asyncio.sleep` instead of `time.sleep`.

    ```solara
    import asyncio
    import solara
    from solara.lab import use_task, TaskResult


    @solara.component
    def Page():
        number = solara.use_reactive(4)

        async def square():
            await asyncio.sleep(1)
            return number.value**2

        result: TaskResult[bool] = use_task(square, dependencies=[number.value])

        solara.InputInt("Square", value=number, continuous_update=True)
        if result.finished:
            solara.Success(f"Square of {number} == {result.value}")
        solara.ProgressLinear(result.pending)
    ```

    ## Arguments

    - `f`: The function or coroutine to run as a task.
    - `dependencies`: A list of dependencies that will trigger a rerun of the task when changed.
    - `raise_error`: If true, an error in the task will be raised. If false, the error should be handled by the
        user and is available in the `.error` attribute of the task object.
    - `prefer_threaded` - bool: Will run coroutine functions as a task in a thread when threads are available.
        This ensures that even when a coroutine functions calls a blocking function the UI is still responsive.
        On platform where threads are not supported (like Pyodide / WASM / Emscripten / PyScript), a coroutine
        function will always run in the current event loop.


    """

    def wrapper(f):
        task_instance = solara.use_memo(lambda: task(f, prefer_threaded=prefer_threaded), dependencies=[])

        def run():
            task_instance()
            return task_instance.cancel

        solara.use_effect(run, dependencies=dependencies)
        if raise_error:
            if task_instance.error:
                raise task_instance.exception
        return task_instance.result

    if f is None:
        return wrapper
    else:
        return wrapper(f)
