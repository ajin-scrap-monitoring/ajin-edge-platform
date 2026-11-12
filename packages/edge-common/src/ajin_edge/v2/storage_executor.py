"""One storage owner with bounded urgent bursts; durability stays with Outbox."""

import heapq
import itertools
import threading
from concurrent.futures import Executor, Future


class StorageExecutor(Executor):
    def __init__(self, *, urgent_burst=4):
        self.condition = threading.Condition()
        self.queue = []
        self.sequence = itertools.count()
        self.stopping = False
        self.urgent_burst = urgent_burst
        self.thread = threading.Thread(target=self._run, name="v2-storage")
        self.thread.start()

    def submit(self, fn, /, *args, **kwargs):
        return self.submit_priority(2, fn, *args, **kwargs)

    def submit_priority(self, priority, fn, /, *args, **kwargs):
        future = Future()
        with self.condition:
            if self.stopping:
                raise RuntimeError("storage executor is shut down")
            heapq.heappush(self.queue, (priority, next(self.sequence), future, fn, args, kwargs))
            self.condition.notify()
        return future

    def _run(self):
        urgent = 0
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.queue or self.stopping)
                if not self.queue:
                    return
                normal = min((r for r in self.queue if r[0] == 2), key=lambda r: r[1], default=None)
                if urgent >= self.urgent_burst and normal is not None:
                    self.queue.remove(normal)
                    heapq.heapify(self.queue)
                    row = normal
                else:
                    row = heapq.heappop(self.queue)
                priority, _, future, fn, args, kwargs = row
            if not future.set_running_or_notify_cancel():
                continue
            urgent = urgent + 1 if priority < 2 else 0
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self.condition:
            self.stopping = True
            if cancel_futures:
                for row in self.queue:
                    row[2].cancel()
                self.queue.clear()
            self.condition.notify_all()
        if wait:
            self.thread.join()
