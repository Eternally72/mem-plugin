"""Timing and dispatch only; periodic scans delegate all decisions to the engine."""

from time import monotonic


class Scheduler:
    def __init__(
        self,
        service,
        contexts,
        intervals,
        batch_size=50,
        *,
        consolidation_enabled=True,
        lifecycle=None,
        lifecycle_interval=3600,
    ):
        self._service, self._contexts = service, contexts
        self._intervals, self._batch = intervals, batch_size
        self._next = [0.0, 0.0, 0.0]
        self._consolidation_enabled = consolidation_enabled
        self._lifecycle, self._lifecycle_interval = lifecycle, lifecycle_interval
        self._next_lifecycle = 0.0

    def run_once(self, *, force=False):
        now = monotonic()
        count = 0
        for index, interval in enumerate(self._intervals if self._consolidation_enabled else ()):
            if force or now >= self._next[index]:
                for context in self._contexts:
                    count += self._service.enqueue_due(context, index + 1, self._batch)
                self._next[index] = now + interval
        if self._lifecycle is not None and (force or now >= self._next_lifecycle):
            for context in self._contexts:
                count += self._lifecycle.schedule(context, self._batch)
            self._next_lifecycle = now + self._lifecycle_interval
        return count

    def request(self, node_id):
        if not self._consolidation_enabled:
            raise ValueError("enable consolidation to request a derived refresh")
        if len(self._contexts) != 1:
            raise ValueError("select a single namespace when requesting a memory")
        self._service.request(self._contexts[0], node_id)
