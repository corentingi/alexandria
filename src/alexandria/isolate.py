"""Parse untrusted files in a child process, with a time and a memory limit.

pypdf, lxml and trafilatura run on files anyone can send. A crafted file can make them
spin or allocate without end, and a thread cannot be stopped: it would hold a worker slot
until the server restarts. A child process can be killed (ADR-0020).

Parent: ``await run_isolated(task, args, limits)``. Child: ``python -m alexandria.isolate``
reads ``{"task": ..., "args": [...]}`` on stdin and writes ``{"ok": result}`` or
``{"error": message}`` on stdout. Only the tasks in ``TASKS`` can run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from alexandria.config import ExtractSettings
from alexandria.extract import ExtractionError


class IsolatedError(ExtractionError):
    """The task failed, ran out of time or memory, or the child died."""


def _extract(path: str, fmt: str, url: str | None, charset: str | None) -> dict[str, Any]:
    from alexandria.extract import extract

    return asdict(extract(Path(path), fmt, url, charset))


def _article(path: str, url: str) -> dict[str, Any]:
    from alexandria.fetchers.article import _extract as extract_article

    markdown, info = extract_article(Path(path).read_bytes(), url)
    return {"markdown": markdown, "info": info}


TASKS: dict[str, Callable[..., dict[str, Any]]] = {"extract": _extract, "article": _article}


async def run_isolated(task: str, args: list[Any], limits: ExtractSettings) -> dict[str, Any]:
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}")
    if not limits.isolate:  # tests and debugging: same code, in a thread
        try:
            return await asyncio.to_thread(TASKS[task], *args)
        except ExtractionError:
            raise
        except Exception as exc:
            raise IsolatedError(str(exc) or type(exc).__name__) from exc
    request = json.dumps({"task": task, "args": args, "memory_mb": limits.max_memory_mb})
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alexandria.isolate",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        async with asyncio.timeout(limits.timeout_s):
            out, err = await proc.communicate(request.encode())
    except TimeoutError:
        raise IsolatedError(f"gave up after {limits.timeout_s:.0f}s") from None
    finally:
        if proc.returncode is None:  # timed out, or the job was cancelled
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    try:
        reply = json.loads(out)
    except ValueError:
        reason = "ran out of memory" if b"MemoryError" in err else "stopped unexpectedly"
        raise IsolatedError(f"the parser {reason} (exit {proc.returncode})") from None
    if "error" in reply:
        raise IsolatedError(reply["error"])
    result: dict[str, Any] = reply["ok"]
    return result


def _limit_memory(mb: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024,) * 2)
    except ImportError, ValueError, OSError:
        pass  # not enforceable here (macOS); the time limit still applies


def main() -> None:
    request = json.loads(sys.stdin.buffer.read())
    _limit_memory(int(request.get("memory_mb") or 0) or 2048)
    try:
        reply: dict[str, Any] = {"ok": TASKS[request["task"]](*request["args"])}
    except MemoryError:
        reply = {"error": "the parser ran out of memory"}
    except Exception as exc:
        reply = {"error": str(exc) or type(exc).__name__}
    sys.stdout.write(json.dumps(reply, default=str))


if __name__ == "__main__":
    main()
