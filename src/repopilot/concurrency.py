"""Bounded parallel execution that keeps results in input order."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar, cast

T = TypeVar("T")
R = TypeVar("R")


def run_ordered(
    func: Callable[[T], R],
    items: Iterable[T],
    *,
    max_workers: int,
    on_result: Callable[[int, R], None] | None = None,
) -> list[R]:
    """Map ``func`` over ``items`` with bounded concurrency.

    Results are returned in input order regardless of completion order, and the
    first failure (by input order, not by completion time) is re-raised after
    every task has settled. ``on_result`` runs under a lock as each task
    completes, so callers can persist incrementally without racing.
    """
    work = list(items)
    workers = max(1, max_workers)
    if workers == 1 or len(work) <= 1:
        results: list[R] = []
        for index, item in enumerate(work):
            value = func(item)
            results.append(value)
            if on_result is not None:
                on_result(index, value)
        return results

    settled: list[R | None] = [None] * len(work)
    errors: dict[int, BaseException] = {}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=min(workers, len(work))) as pool:
        futures = {pool.submit(func, item): index for index, item in enumerate(work)}
        for future in as_completed(futures):
            index = futures[future]
            failure = future.exception()
            if failure is not None:
                errors[index] = failure
                continue
            value = future.result()
            settled[index] = value
            if on_result is not None:
                with lock:
                    on_result(index, value)
    if errors:
        raise errors[min(errors)]
    return [cast(R, value) for value in settled]
