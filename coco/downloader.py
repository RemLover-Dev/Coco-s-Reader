from __future__ import annotations

import asyncio
import inspect
import random
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, unquote

import httpx


DEFAULT_DOWNLOAD_DIR = Path("~/Downloads/CocoDownloads").expanduser().absolute()
DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class DownloadProgress:
    done: int
    total: int
    failed: int
    skipped: int
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


def _unique_path(path: Path) -> Path:
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


def _create_cbz_from_files(files: list[Path], cbz_path: Path) -> Path:
    cbz_path.parent.mkdir(parents=True, exist_ok=True)

    # Overwrite if it already exists
    if cbz_path.exists():
        cbz_path.unlink()

    with zipfile.ZipFile(cbz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            # Use only the filename inside the archive
            zf.write(file_path, arcname=file_path.name)

    return cbz_path


async def download_urls(
    client: httpx.AsyncClient,
    urls: list[str],
    concurrent_downloads: int,
    download_dir: str | Path = DEFAULT_DOWNLOAD_DIR,
    progress_callback: Optional[
        Callable[[DownloadProgress], Awaitable[None] | None]
    ] = None,
    retries: int = 3,
    backoff_base: float = 0.5,
    skip_existing: bool = False,
    create_cbz: bool = False,
    cbz_name: str | None = None,
) -> tuple[list[Path], Path | None]:
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
    skipped = 0

    results: list[Path | None] = [None] * total

    async def emit(
        url: str | None = None,
        filename: str | None = None,
        error: Exception | None = None,
    ) -> None:
        if progress_callback is None:
            return

        event = DownloadProgress(
            done=done,
            total=total,
            failed=failed,
            skipped=skipped,
            url=url,
            filename=filename,
            error=error,
        )

        maybe = progress_callback(event)
        if inspect.isawaitable(maybe):
            await maybe

    async def download_one(index: int, url: str) -> None:
        nonlocal done, failed, skipped

        async with semaphore:
            last_error: Exception | None = None

            for attempt in range(retries + 1):
                try:
                    fallback_name = f"file_{index}"
                    filename = _safe_filename_from_url(url, fallback_name)
                    base_path = download_dir / filename

                    # Optional "skip if already exists"
                    if skip_existing and base_path.exists():
                        async with lock:
                            done += 1
                            skipped += 1
                            results[index] = base_path
                            await emit(url=url, filename=base_path.name, error=None)
                        return

                    # Otherwise allow duplicate downloads by generating a unique path
                    final_path = _unique_path(base_path)
                    tmp_path = final_path.with_name(final_path.name + ".part")

                    async with client.stream("GET", url) as response:
                        response.raise_for_status()

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

                    async with lock:
                        done += 1
                        results[index] = final_path
                        await emit(url=url, filename=final_path.name, error=None)
                    return

                except (httpx.HTTPStatusError, httpx.RequestError, OSError) as e:
                    last_error = e

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

            async with lock:
                done += 1
                failed += 1
                await emit(url=url, error=last_error)

    await emit()
    await asyncio.gather(*(download_one(i, url) for i, url in enumerate(urls)))

    downloaded_files = [p for p in results if p is not None]

    cbz_path: Path | None = None
    if create_cbz and downloaded_files:
        if cbz_name is None:
            cbz_name = f"{download_dir.name}.cbz"
        cbz_path = _create_cbz_from_files(downloaded_files, download_dir / cbz_name)

    return downloaded_files, cbz_path