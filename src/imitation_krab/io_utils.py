from __future__ import annotations

import os


def write_all(fd: int, data: bytes) -> None:
    """Write every byte or raise without silently accepting short writes."""
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0 or written > len(remaining):
            raise OSError("file write made no progress")
        remaining = remaining[written:]
