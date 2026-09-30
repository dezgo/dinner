"""Background work (photo reading, bank sync) off the request thread.

A small thread pool. Tests switch it to run inline so results are immediate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dinnertab-job")
run_inline = False


def submit(fn: Callable, *args) -> None:
    def wrapped() -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("background job %s failed", getattr(fn, "__name__", fn))

    if run_inline:
        wrapped()
    else:
        _pool.submit(wrapped)
