from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from coco.provider import ComicInfo, Provider
from coco.downloader import download_urls


def safe_name(text: str, fallback: str = "item") -> str:
    text = text.strip()
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or fallback


@dataclass(slots=True, frozen=True)
class SeriesRecord:
    provider_code: str
    series_id: str
    title: str
    url: str
    local_dir: str
    thumbnail_url: str = ""
    is_nsfw: int = 0
    last_seen: float = 0.0


@dataclass(slots=True, frozen=True)
class ChapterRecord:
    provider_code: str
    series_id: str
    chapter_id: str
    title: str
    url: str
    local_path: str
    downloaded_at: float = 0.0
    last_seen: float = 0.0
    exists: int = 1


class LibraryIndex:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS series (
                    provider_code TEXT NOT NULL,
                    series_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    local_dir TEXT NOT NULL,
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    is_nsfw INTEGER NOT NULL DEFAULT 0,
                    last_seen REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (provider_code, series_id)
                );

                CREATE TABLE IF NOT EXISTS chapters (
                    provider_code TEXT NOT NULL,
                    series_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    downloaded_at REAL NOT NULL DEFAULT 0,
                    last_seen REAL NOT NULL DEFAULT 0,
                    exists INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (provider_code, series_id, chapter_id),
                    FOREIGN KEY (provider_code, series_id)
                        REFERENCES series(provider_code, series_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_chapters_series
                ON chapters(provider_code, series_id);

                CREATE INDEX IF NOT EXISTS idx_chapters_exists
                ON chapters(exists);
                """
            )

    def upsert_series(self, record: SeriesRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO series (
                    provider_code, series_id, title, url, local_dir,
                    thumbnail_url, is_nsfw, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_code, series_id) DO UPDATE SET
                    title=excluded.title,
                    url=excluded.url,
                    local_dir=excluded.local_dir,
                    thumbnail_url=excluded.thumbnail_url,
                    is_nsfw=excluded.is_nsfw,
                    last_seen=excluded.last_seen
                """,
                (
                    record.provider_code,
                    record.series_id,
                    record.title,
                    record.url,
                    record.local_dir,
                    record.thumbnail_url,
                    record.is_nsfw,
                    record.last_seen,
                ),
            )

    def upsert_chapter(self, record: ChapterRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chapters (
                    provider_code, series_id, chapter_id, title, url,
                    local_path, downloaded_at, last_seen, exists
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_code, series_id, chapter_id) DO UPDATE SET
                    title=excluded.title,
                    url=excluded.url,
                    local_path=excluded.local_path,
                    downloaded_at=excluded.downloaded_at,
                    last_seen=excluded.last_seen,
                    exists=excluded.exists
                """,
                (
                    record.provider_code,
                    record.series_id,
                    record.chapter_id,
                    record.title,
                    record.url,
                    record.local_path,
                    record.downloaded_at,
                    record.last_seen,
                    record.exists,
                ),
            )

    def get_series(self, provider_code: str, series_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM series
                WHERE provider_code = ? AND series_id = ?
                """,
                (provider_code, series_id),
            ).fetchone()

    def get_chapter(
        self, provider_code: str, series_id: str, chapter_id: str
    ) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM chapters
                WHERE provider_code = ? AND series_id = ? AND chapter_id = ?
                """,
                (provider_code, series_id, chapter_id),
            ).fetchone()

    def list_series(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM series ORDER BY title").fetchall()

    def list_chapters(self, provider_code: str, series_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM chapters
                WHERE provider_code = ? AND series_id = ?
                ORDER BY downloaded_at DESC
                """,
                (provider_code, series_id),
            ).fetchall()

    def reconcile_files(self) -> tuple[int, int]:
        """
        Returns: (exists_count, missing_count)
        Updates the 'exists' flag in the database.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT provider_code, series_id, chapter_id, local_path FROM chapters"
            ).fetchall()

            exists_count = 0
            missing_count = 0

            for row in rows:
                exists = Path(row["local_path"]).exists()
                conn.execute(
                    """
                    UPDATE chapters
                    SET exists = ?
                    WHERE provider_code = ? AND series_id = ? AND chapter_id = ?
                    """,
                    (
                        1 if exists else 0,
                        row["provider_code"],
                        row["series_id"],
                        row["chapter_id"],
                    ),
                )
                if exists:
                    exists_count += 1
                else:
                    missing_count += 1

            return exists_count, missing_count


class LibraryManager:
    def __init__(
        self,
        *,
        library_root: str | Path,
        index: LibraryIndex,
        keep_page_images: bool = False,
        concurrent_downloads: int = 8,
    ) -> None:
        self.library_root = Path(library_root)
        self.index = index
        self.keep_page_images = keep_page_images
        self.concurrent_downloads = concurrent_downloads

        self.library_root.mkdir(parents=True, exist_ok=True)

    def _series_dir(self, provider_code: str, series: ComicInfo) -> Path:
        return self.library_root / provider_code / safe_name(series.title, "series")

    async def refresh_index(self) -> tuple[int, int]:
        return await asyncio.to_thread(self.index.reconcile_files)

    async def register_series(
        self,
        provider_code: str,
        series: ComicInfo,
        *,
        is_nsfw: bool = False,
    ) -> Path:
        series_dir = self._series_dir(provider_code, series)
        await asyncio.to_thread(
            self.index.upsert_series,
            SeriesRecord(
                provider_code=provider_code,
                series_id=str(series.identifier),
                title=series.title,
                url=series.url,
                local_dir=str(series_dir),
                thumbnail_url=series.thumbnail_url,
                is_nsfw=1 if is_nsfw else 0,
                last_seen=time.time(),
            ),
        )
        series_dir.mkdir(parents=True, exist_ok=True)
        return series_dir

    async def sync_series(
        self,
        provider: Provider,
        series: ComicInfo,
        *,
        provider_code: str,
        force_redownload: bool = False,
    ) -> list[Path]:
        """
        Compares remote chapters vs local index and downloads only missing ones
        unless force_redownload=True.
        """
        series_dir = await self.register_series(
            provider_code,
            series,
            is_nsfw=getattr(provider, "is_nsfw", False),
        )

        remote_chapters = await provider.get_chapter_list(series)
        downloaded_paths: list[Path] = []

        for chapter_index, chapter in enumerate(remote_chapters, start=1):
            chapter_id = str(chapter.identifier)
            existing = await asyncio.to_thread(
                self.index.get_chapter,
                provider_code,
                str(series.identifier),
                chapter_id,
            )

            if existing is not None and not force_redownload:
                local_path = Path(existing["local_path"])
                if local_path.exists():
                    continue

            chapter_title = safe_name(chapter.title, f"chapter_{chapter_index:03d}")
            chapter_cbz_name = f"{chapter_index:03d} - {chapter_title}.cbz"
            chapter_cbz_path = series_dir / chapter_cbz_name

            image_urls = await provider.get_chapters_images(chapter)

            # Download images into a temporary chapter folder.
            temp_dir = series_dir / ".pages" / chapter_id
            temp_dir.mkdir(parents=True, exist_ok=True)

            page_paths, cbz_path = await download_urls(
                client=provider.client,
                urls=image_urls,
                concurrent_downloads=self.concurrent_downloads,
                download_dir=temp_dir,
                create_cbz=True,
                cbz_name=chapter_cbz_name,
            )

            # Move CBZ to final location if the downloader created it in temp_dir.
            if cbz_path is not None and cbz_path != chapter_cbz_path:
                chapter_cbz_path.parent.mkdir(parents=True, exist_ok=True)
                if chapter_cbz_path.exists():
                    chapter_cbz_path.unlink()
                cbz_path.replace(chapter_cbz_path)
            else:
                chapter_cbz_path = cbz_path or chapter_cbz_path

            if not self.keep_page_images:
                for p in page_paths:
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass

                # Remove now-empty temp dirs if possible.
                try:
                    for parent in [temp_dir, temp_dir.parent]:
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                except OSError:
                    pass

            if chapter_cbz_path is None:
                continue

            await asyncio.to_thread(
                self.index.upsert_chapter,
                ChapterRecord(
                    provider_code=provider_code,
                    series_id=str(series.identifier),
                    chapter_id=chapter_id,
                    title=chapter.title,
                    url=chapter.url,
                    local_path=str(chapter_cbz_path),
                    downloaded_at=time.time(),
                    last_seen=time.time(),
                    exists=1 if chapter_cbz_path.exists() else 0,
                ),
            )

            downloaded_paths.append(chapter_cbz_path)

        return downloaded_paths

    async def missing_chapters(
        self, provider_code: str, series_id: str
    ) -> list[sqlite3.Row]:
        await self.refresh_index()
        rows = await asyncio.to_thread(
            self.index.list_chapters, provider_code, series_id
        )
        return [row for row in rows if int(row["exists"]) == 0]

    async def downloaded_chapters(
        self, provider_code: str, series_id: str
    ) -> list[sqlite3.Row]:
        await self.refresh_index()
        rows = await asyncio.to_thread(
            self.index.list_chapters, provider_code, series_id
        )
        return [row for row in rows if int(row["exists"]) == 1]