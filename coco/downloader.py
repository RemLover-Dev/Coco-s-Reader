from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, unquote

import httpx


DEFAULT_DOWNLOAD_DIR = Path("~/Downloads/CocoDownlaods").expanduser().absolute()
DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class DownloadProgress:
    done: int
    total: int
    failed: int
    url: str | None = None
    filename: str | None = None
    error: Exception | None = None


def _safe_filename_from_url(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)

    if not name:
        name = fallback

    # remove path traversal and separators
    name = name.replace("/", "_").replace("\\", "_").strip()

    # avoid empty names
    return name or fallback


async def download_urls(
    client: httpx.AsyncClient,
    urls: list[str],
    concurrent_downloads: int,
    download_dir: str | Path = DEFAULT_DOWNLOAD_DIR,
    progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    retries: int = 3,
    backoff_base: float = 0.5,
) -> list[Path]:
    if concurrent_downloads < 1:
        raise ValueError("concurrent_downloads must be >= 1")
    if retries < 0:
        raise ValueError("retries must be >= 0")
    if backoff_base < 0:
        raise ValueError("backoff_base must be >= 0")

    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(concurrent_downloads)
    lock = asyncio.Lock()

    total = len(urls)
    done = 0
    failed = 0

    results: list[Path | None] = [None] * total

    # creates a DownloadProgress object with current information and calls the callback function and passes it to it
    async def emit(url: str | None = None, filename: str | None = None, error: Exception | None = None) -> None:
        if progress_callback is not None:
            event = DownloadProgress(
                done=done,
                total=total,
                failed=failed,
                url=url,
                filename=filename,
                error=error,
            )
            # in case the callback function is an async one
            maybe = progress_callback(event)
            if asyncio.iscoroutine(maybe):
                await maybe

    def unique_path(path: Path) -> Path:
        if not path.exists():
            return path

        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        i = 1
        while True:
            candidate = parent / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    async def download_one(index: int, url: str) -> None:
        nonlocal done, failed

        async with semaphore:
            last_error: Exception | None = None

            for attempt in range(retries + 1):
                try:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()

                        fallback_name = f"file_{index}"
                        filename = _safe_filename_from_url(url, fallback_name)
                        final_path = unique_path(download_dir / filename)
                        tmp_path = final_path.with_name(final_path.name + ".part")

                        try:
                            if tmp_path.exists():
                                tmp_path.unlink()

                            with tmp_path.open("wb") as f:
                                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                    if chunk:
                                        f.write(chunk)

                            tmp_path.replace(final_path)
                        except Exception:
                            if tmp_path.exists():
                                try:
                                    tmp_path.unlink()
                                except OSError:
                                    pass
                            raise

                        results[index] = final_path

                        async with lock:
                            done += 1
                            await emit(url=url, filename=final_path.name, error=None)
                        return

                except (httpx.HTTPStatusError, httpx.RequestError, OSError) as e:
                    last_error = e

                    # retry only if we still have attempts left
                    if attempt < retries:
                        delay = backoff_base * (2 ** attempt)
                        delay += random.uniform(0, min(0.25, delay / 4 if delay else 0.25))
                        await asyncio.sleep(delay)
                        continue

                    async with lock:
                        done += 1
                        failed += 1
                        await emit(url=url, filename=None, error=e)
                    return

            # defensive fallback; should never be reached
            async with lock:
                done += 1
                failed += 1
                await emit(url=url, error=last_error)

    await emit()
    await asyncio.gather(*(download_one(i, url) for i, url in enumerate(urls)))

    return [p for p in results if p is not None]