# COCO Core Library — AI Integration Guide

This document is a complete, self-contained reference for building on the
**coco** core library. It is written so that an AI agent (or developer) can
build a CLI, GUI, or new provider against this library **without reading the
source code**. All public names, signatures, and behaviors below are taken
directly from the codebase.

---

## 1. What this library is

`coco` is an **async Python library** for downloading manga/manhwa/doujin
("eastern content") from multiple sources. It has three layers:

| Layer | Module | Purpose |
|-------|--------|---------|
| Providers | `coco.provider`, `coco.providers.*` | Fetch/search comics and their page image URLs |
| Downloader | `coco.downloader` | Download image URLs concurrently, optionally pack to CBZ |
| Library | `coco.library_index` | Track series/chapters on disk in SQLite, sync what's missing |

Everything network-facing is **async** (`async def`) and must be run inside
`asyncio`.

---

## 2. Runtime requirements

From `pyproject.toml`:

- **Python >= 3.12** (the code uses `match`/`case`, PEP 604 unions, `typing.Self`)
- Runtime dependencies: `httpx`, `beautifulsoup4`

Provider implementations parse HTML with `bs4.BeautifulSoup` and use
`httpx.AsyncClient` for HTTP.

**Import convention:** all imports are absolute from the package root, e.g.:

```python
from coco.provider import Provider, ComicInfo, ComicChapter
from coco.providers.weebcentral import Weebcentral
from coco.downloader import download_urls
from coco.library_index import LibraryIndex, LibraryManager
from coco.provider_registry import provider_registry, ProviderRegistry
```

---

## 3. Core data models

Defined in `coco.provider`.

### 3.1 `ComicInfo` (a series/search result)

```
@dataclass(slots=True)
class ComicInfo:
    title: str                 # REQUIRED, no default
    status: str                # REQUIRED, no default
    tags: list[str] = field(default_factory=list)
    identifier: str = ""       # stable provider-specific ID (use as a key)
    thumbnail_url: str = ""
    url: str = ""              # may be relative for some providers
    authors: list[str] = field(default_factory=list)
    release_year: str = ""     # string, not int
    item_type: str = ""        # e.g. "manga", "doujin"
```

- `title` and `status` are **positional-or-keyword and required** — always
pass them when constructing.
- `identifier` is the stable key you should use for equality, storage, and
lookups. It is NOT necessarily numeric and NOT a URL.

### 3.2 `ComicChapter` (a chapter of a series)

```
@dataclass(slots=True)
class ComicChapter:
    title: str        # REQUIRED
    url: str          # REQUIRED
    identifier: str   # REQUIRED, unique per chapter within the series
```

---

## 4. The `Provider` base class

Defined in `coco.provider`. All providers subclass it.

### 4.1 Class-level configuration

```
class Provider(ABC):
    name: str = ""           # human-readable name
    code_name: str = ""      # short slug used as registry key, e.g. "wc"
    max_retries: int = 3     # retries for safe_request
    referer: str = ""        # site base URL used for headers and URL building
    timeout: float = 5.0     # default httpx timeout (seconds)
```

Subclasses may override these (e.g. Toonily sets `timeout = 30.0`,
`max_retries = 5`).

### 4.2 Constructor

```
def __init__(
    self,
    proxy: str = "",
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float | None = None,
    follow_redirects: bool = True,
    headers: dict[str, str] | None = None,
) -> None
```

- If `client` is `None`, the provider **creates and owns** an
`httpx.AsyncClient`; otherwise it reuses the one you pass (caller manages
its lifecycle).
- `client` is exposed as the public attribute `provider.client` — the
downloader needs it.
- On init, random browser-like headers are injected via
`generate_random_headers(self.referer)`.

### 4.3 Lifecycle

Providers support the async context manager protocol and manual `close()`:

```
async with Weebcentral() as p:      # auto-closes
    ...

p = Weebcentral()                   # manual
try:
    ...
finally:
    await p.close()                 # only closes if the provider owns the client
```

`close()` is a no-op if you passed your own `client`.

### 4.4 Request helpers

```
async def safe_request(
    self,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
) -> httpx.Response

async def safe_get(
    self,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response
```

- Both **return an `httpx.Response` only when status is 200**. Call
`.text` (HTML) or `.json()` (JSON APIs) on the result.
- Retry semantics: 5xx responses and network/timeout errors are retried with
exponential backoff (`min(2**(attempt-1), 8)` seconds). 4xx errors raise
`RequestError` immediately.
- On exhaustion, `safe_request` raises `RequestError` carrying `.url`,
`.status_code`, `.attempts`, and a chained `cause`.

### 4.5 Abstract methods every provider must implement

```
async def search(self, query: str) -> list[ComicInfo]: ...
async def get_chapter_list(self, item: ComicInfo) -> list[ComicChapter]: ...
async def get_chapters_images(self, item: ComicChapter) -> list[str]: ...
```

> `get_chapters_images` returns **final, absolute image URLs** ready to hand
> to the downloader.

---

## 5. Exceptions

Defined in `coco.provider`:

```
class ProviderError(Exception): ...            # base
class RequestError(ProviderError): ...         # HTTP/network failure
class ParseError(ProviderError): ...           # HTML/JSON didn't match expectations
```

- `RequestError` attributes: `url`, `status_code`, `attempts`, and
`__cause__` (the underlying exception).
- `ParseError` signals the site structure changed or the expected elements
weren't found. Providers raise it when parsing fails (e.g. Toonily when it
can't find the chapter list/images).

**Robust code should catch `ProviderError` broadly**, then optionally
inspect the concrete subtype.

---

## 6. Built-in providers

Located in `coco/providers/`. Import the class, instantiate, and call the
abstract methods.

### 6.1 `Weebcentral` — `coco.providers.weebcentral`

- Class attrs: `name="Weebcentral"`, `code_name="wc"`,
`referer="https://weebcentral.com"`.
- `search(query)` uses the JSON search endpoint (`/search/data`) and parses
results out of `<article class="bg-base-300">` elements.
- `get_chapter_list(item)` reads
`/series/{item.identifier}/full-chapter-list`.
- `get_chapters_images(item)` reads
`/chapters/{item.identifier}/images?...&reading_style=long_strip`.
- Note: `ComicInfo.url` returned by search may be a **relative path**
(`/series/...`). Treat `identifier` as the source of truth.

### 6.2 `Toonily` — `coco.providers.toonily`

- Class attrs: `name="toonily"`, `code_name="tl"`,
`referer="https://toonily.com"`, `timeout=30.0`, `max_retries=5`.
- `search(query)` first tries the WordPress AJAX endpoint via POST, then
falls back to HTML scraping (`/ ?s={query}`).
- `get_chapter_list(item)` uses `item.url` (must be the absolute series URL)
and parses the chapter `<ul>`.
- `get_chapters_images(item)` uses `item.url` and reads
`.reading-content img.wp-manga-chapter-img` (prefers `data-src`, falls back
to `src`; dedupes URLs).

### 6.3 `Nhentai` — `coco.providers.nhentai`

- Class attrs: `name="NHentai"`, `code_name="nh"`,
`referer="https://nhentai.net/api/v2"`.
- **Signature differs from the base** (extra keyword args with defaults):

```
async def search(self, query: str, sort: str = "popular", page: int = 1) -> list[ComicInfo]
```

Python's `abc` only enforces method *existence*, so this override is fine,
but code calling `provider.search(q)` works unchanged.
- `get_cdn() -> dict[str, str]` returns the CDN server map
(`thumb_servers`, `image_servers`).
- `get_chapter_list(item)` returns a **single** `ComicChapter` (nhentai has
no chapters; this keeps the abstraction uniform).
- `get_chapters_images(item)` builds URLs from the CDN `image_servers`.

---

## 7. Provider registry

Defined in `coco.provider_registry`.

```
@dataclass(slots=True, frozen=True)
class ProviderRegistry:
    title: str
    code_name: str
    base_url: str
    url_detection_regex: Pattern[str]
    is_nsfw: bool
```

The module-level `provider_registry: dict[str, ProviderRegistry]` maps the
`code_name` to its entry:

| Key ↕▾ | title ↕▾ | is_nsfw ↕▾ | base_url ↕▾ |
|---|---|---|---|
| −`wc` | Weebcentral | `False` | `https://weebcentral.com` |
| −`tl` | Toonily | `True` | `https://toonily.com` |
| −`nh` | Nhentai | `True` | `https://nhentai.net` |
⚙

Use `url_detection_regex` to decide which provider handles a URL the user
pastes in, and `is_nsfw` for content warnings.

> **Important:** NSFW state lives in the **registry**, NOT on provider
> instances. The `Provider` base class does not define an `is_nsfw`
> attribute. `LibraryManager.sync_series` reads
> `getattr(provider, "is_nsfw", False)` and will therefore record **False**
> unless you register series with an explicit `is_nsfw=True` (see §9).

---

## 8. Downloader

Defined in `coco.downloader`.

### 8.1 Constants & progress event

```
DEFAULT_DOWNLOAD_DIR = Path("~/Downloads/CocoDownloads").expanduser().absolute()
```

```
@dataclass(slots=True)
class DownloadProgress:
    done: int
    total: int
    failed: int
    skipped: int
    url: str | None = None
    filename: str | None = None
    error: Exception | None = None
```

### 8.2 `download_urls`

```
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
) -> tuple[list[Path], Path | None]
```

Behavior:

- Downloads all `urls` concurrently (bounded by a semaphore of size
`concurrent_downloads`).
- Validates `concurrent_downloads >= 1`, `retries >= 0`,
`backoff_base >= 0`, raising `ValueError` otherwise.
- Writes to `<name>.part` temp files then atomically renames on success.
Failed downloads are cleaned up.
- Filenames are derived from the URL's last path segment; duplicates get an
`_1`, `_2` suffix via `_unique_path`. If `skip_existing=True`, an
already-present base filename is skipped instead of duplicated.
- `progress_callback` may be **sync or async**; it is awaited if it returns
an awaitable. It fires once at start and once per completed URL.
- **Returns `(downloaded_files, cbz_path)`**:

- `downloaded_files`: `list[Path]` of every successfully downloaded file
(or a pre-existing one, when skipped).
- `cbz_path`: `Path | None` — set only when `create_cbz=True` and at least
one file was downloaded. The CBZ stores files with **flat arcnames**
(just the filename, no directories).
- When `create_cbz=True` and `cbz_name` is `None`, the archive is named
`"{download_dir.name}.cbz"`.

---

## 9. Library (persistent index)

Defined in `coco.library_index`. Two layers:

- **`LibraryIndex`** — thin SQLite wrapper (blocking, synchronous).
- **`LibraryManager`** — async orchestration that ties providers + downloader

- index together.

### 9.1 Data records

```
@dataclass(slots=True, frozen=True)
class SeriesRecord:
    provider_code: str
    series_id: str
    title: str
    url: str
    local_dir: str
    thumbnail_url: str = ""
    is_nsfw: int = 0        # 0/1, not bool
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
    exists: int = 1          # 0/1
```

### 9.2 `LibraryIndex`

```
class LibraryIndex:
    def __init__(self, db_path: str | Path) -> None: ...
    def initialize(self) -> None: ...          # CREATE TABLE IF NOT EXISTS ...
    def upsert_series(self, record: SeriesRecord) -> None: ...
    def upsert_chapter(self, record: ChapterRecord) -> None: ...
    def get_series(self, provider_code: str, series_id: str) -> sqlite3.Row | None: ...
    def get_chapter(self, provider_code: str, series_id: str, chapter_id: str) -> sqlite3.Row | None: ...
    def list_series(self) -> list[sqlite3.Row]: ...
    def list_chapters(self, provider_code: str, series_id: str) -> list[sqlite3.Row]: ...
    def reconcile_files(self) -> tuple[int, int]: ...
```

- Call `initialize()` once before any other use.
- Row access is by column name (`row["title"]`) because `row_factory` is
`sqlite3.Row`.
- `reconcile_files()` checks each chapter's `local_path` on disk, updates
`exists`, and returns `(exists_count, missing_count)`.
- `list_chapters` orders by `downloaded_at DESC`.
- Keys: series primary key is `(provider_code, series_id)`; chapters add
`chapter_id`. In practice, `series_id = str(comic.identifier)` and
`chapter_id = str(chapter.identifier)`.

### 9.3 `LibraryManager`

```
class LibraryManager:
    def __init__(
        self,
        *,
        library_root: str | Path,
        index: LibraryIndex,
        keep_page_images: bool = False,
        concurrent_downloads: int = 8,
    ) -> None: ...
```

- All constructor params except `self` are **keyword-only**.
- `library_root` is created if missing.

Methods:

```
async def refresh_index(self) -> tuple[int, int]: ...
async def register_series(self, provider_code: str, series: ComicInfo, *, is_nsfw: bool = False) -> Path: ...
async def sync_series(self, provider: Provider, series: ComicInfo, *, provider_code: str, force_redownload: bool = False) -> list[Path]: ...
async def missing_chapters(self, provider_code: str, series_id: str) -> list[sqlite3.Row]: ...
async def downloaded_chapters(self, provider_code: str, series_id: str) -> list[sqlite3.Row]: ...
```

`sync_series` is the main workhorse:

1. Registers the series (creating `<library_root>/<provider_code>/<safe_title>/`).
2. Fetches the remote chapter list.
3. For each chapter not already present (or all, if `force_redownload=True`),
fetches image URLs and downloads them via `download_urls` **using
`provider.client`** into a temp `.pages/<chapter_id>` dir, packs a CBZ,
moves it to the series dir as `NNN - <title>.cbz`, and records a
`ChapterRecord`.
4. Returns the list of newly downloaded CBZ `Path`s.

Notes:

- `provider` passed to `sync_series` must expose `.client` (any `Provider`
subclass does).
- By default page images are deleted after CBZ packing; set
`keep_page_images=True` to retain them.
- Series dirs are sanitized with `safe_name(text, fallback)` from this
module (removes `<>:"/\|?*` and control chars, collapses whitespace).

---

## 10. End-to-end examples

### 10.1 Search → chapters → images

```
import asyncio
from coco.providers.weebcentral import Weebcentral

async def main():
    async with Weebcentral() as provider:
        results = await provider.search("solo leveling")
        if not results:
            return
        comic = results[0]
        print(comic.title, "|", comic.identifier)

        chapters = await provider.get_chapter_list(comic)
        first = chapters[0]

        images = await provider.get_chapters_images(first)
        print(len(images), "pages")

asyncio.run(main())
```

### 10.2 Download one chapter to a CBZ

```
import asyncio
from coco.providers.weebcentral import Weebcentral
from coco.downloader import download_urls, DownloadProgress

async def on_progress(p: DownloadProgress):
    print(f"{p.done}/{p.total} failed={p.failed} skipped={p.skipped}")

async def main():
    async with Weebcentral() as provider:
        comic = (await provider.search("one piece"))[0]
        chapter = (await provider.get_chapter_list(comic))[0]
        images = await provider.get_chapters_images(chapter)

        files, cbz = await download_urls(
            client=provider.client,
            urls=images,
            concurrent_downloads=8,
            download_dir="downloads/one_piece_ch1",
            progress_callback=on_progress,
            create_cbz=True,
            cbz_name="001.cbz",
        )
        print("CBZ:", cbz)

asyncio.run(main())
```

### 10.3 Keep a whole series in sync on disk

```
import asyncio
from coco.library_index import LibraryIndex, LibraryManager
from coco.providers.weebcentral import Weebcentral

async def main():
    index = LibraryIndex("library/coco.db")
    index.initialize()
    manager = LibraryManager(library_root="library", index=index, concurrent_downloads=8)

    async with Weebcentral() as provider:
        comic = (await provider.search("solo leveling"))[0]
        new_paths = await manager.sync_series(provider, comic, provider_code="wc")
        print("Downloaded:", new_paths)

        # Later: list what's missing / present
        missing = await manager.missing_chapters("wc", str(comic.identifier))
        present = await manager.downloaded_chapters("wc", str(comic.identifier))
        print(len(missing), "missing;", len(present), "present")

asyncio.run(main())
```

---

## 11. Writing a custom provider

To add a new source, create `coco/providers/<name>.py`:

```
from coco.provider import Provider, ComicInfo, ComicChapter, ParseError

class MyProvider(Provider):
    name = "My Site"
    code_name = "ms"
    referer = "https://example.com"   # base URL
    # optionally: timeout = 10.0, max_retries = 5

    async def search(self, query: str) -> list[ComicInfo]:
        resp = await self.safe_get(f"{self.referer}/search", params={"q": query})
        # parse resp.text (HTML) or resp.json() (API)
        # build and return list[ComicInfo]; title and status REQUIRED
        ...

    async def get_chapter_list(self, item: ComicInfo) -> list[ComicChapter]:
        # use item.identifier and/or item.url
        # build list[ComicChapter]; all three fields REQUIRED
        ...

    async def get_chapters_images(self, item: ComicChapter) -> list[str]:
        # return ABSOLUTE image URLs
        ...
```

**Contract rules:**

1. Subclass `Provider` and implement exactly the three abstract methods.
2. Set `name`, `code_name`, `referer` as class attributes.
3. Use `safe_get`/`safe_request` for fetches — they handle retries and
produce `httpx.Response` on 200.
4. Raise `ParseError` when the expected DOM/JSON shape is missing.
5. Fill `ComicInfo.identifier` with a stable, unique-per-series value; do the
same for `ComicChapter.identifier`.
6. Optionally register it in `coco/provider_registry.py`:

```
provider_registry["ms"] = ProviderRegistry(
    title="My Site",
    code_name="ms",
    base_url="https://example.com",
    url_detection_regex=re.compile(r"^https?://(?:www\.)?example\.com(?:/|$)"),
    is_nsfw=False,
)
```

No central plugin mechanism exists — registration is manual in that dict.

---

## 12. Conventions, gotchas, and pitfalls

- **Everything is async.** Run via `asyncio.run(main())`; never call these
coroutines from sync code without an event loop.
- **Providers are not thread-safe by design**; they wrap a shared
`httpx.AsyncClient`. Share one provider across tasks if you want connection
reuse, but serialize its use if you aren't sure.
- **Lifecycle:** use `async with Provider() as p:` or explicitly
`await p.close()`. If you pass your own `client`, YOU own closing it.
- **`identifier` is your stable key**, not `url` and not the list index.
Some providers return relative URLs (`Weebcentral` search results).
- **`ComicInfo.status` is required** — provide a placeholder like
`"Unknown"` if a site doesn't expose status (see `Toonily`).
- **`Nhentai.search` has an extended signature** (`sort`, `page`). Generic
code should call `provider.search(query)` with one positional arg and
ignore extras.
- **NSFW flag:** consult `provider_registry[code_name].is_nsfw`, and pass
`is_nsfw=...` explicitly to `LibraryManager.register_series` if you need
the DB record to reflect it. `sync_series` cannot infer it automatically.
- **`DEFAULT_DOWNLOAD_DIR` contains the typo "CocoDownlaods".** Prefer
passing your own `download_dir`.
- **`safe_request` raises `RequestError` on 4xx immediately** and on 5xx only
after retries are exhausted. Catch `ProviderError` for graceful UI/CLI
handling.
- **JSON responses:** `safe_get(...).json()` (httpx handles decoding). HTML
responses: `safe_get(...).text`.
- **CBZ archives are flat:** page images are stored by filename only inside
the archive, in the order they were downloaded (which preserves
`get_chapters_images` ordering, but filename sort order is not guaranteed
— prefer zero-padded source filenames when they exist).

---

## 13. Quick API cheat sheet

```
# Provider
Provider.search(query) -> list[ComicInfo]
Provider.get_chapter_list(ComicInfo) -> list[ComicChapter]
Provider.get_chapters_images(ComicChapter) -> list[str]
Provider.safe_get(url, params=None, *, headers=None) -> httpx.Response   # 200 only
Provider.safe_request(url, params=None, *, headers=None, method="GET") -> httpx.Response
Provider.close() / async with

# Exceptions
ProviderError, RequestError, ParseError   # from coco.provider

# Downloader
download_urls(client, urls, concurrent_downloads, download_dir=..., progress_callback=...,
              retries=3, backoff_base=0.5, skip_existing=False,
              create_cbz=False, cbz_name=None) -> (list[Path], Path | None)
DownloadProgress(done, total, failed, skipped, url=None, filename=None, error=None)

# Library
LibraryIndex(db_path).initialize()
LibraryIndex.upsert_series(SeriesRecord) / .upsert_chapter(ChapterRecord)
LibraryIndex.list_series() / .list_chapters(provider_code, series_id)
LibraryIndex.get_series(pc, sid) / .get_chapter(pc, sid, cid)
LibraryIndex.reconcile_files() -> (exists_count, missing_count)
LibraryManager(*, library_root, index, keep_page_images=False, concurrent_downloads=8)
LibraryManager.register_series(pc, series, *, is_nsfw=False) -> Path
LibraryManager.sync_series(provider, series, *, provider_code, force_redownload=False) -> list[Path]
LibraryManager.missing_chapters(pc, sid) / .downloaded_chapters(pc, sid) -> list[sqlite3.Row]

# Registry
provider_registry: dict[str, ProviderRegistry]   # keys: wc, tl, nh
safe_name(text, fallback="item") -> str          # from coco.library_index
```

