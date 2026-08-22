import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from coco.provider import ComicChapter, ComicInfo, ParseError, Provider, RequestError


class Toonily(Provider):
    name = "toonily"
    referer = "https://toonily.com"
    code_name = "tl"
    timeout = 30.0
    max_retries = 5

    def _abs_url(self, url: str) -> str:
        return urljoin(self.referer, url)

    def _identifier_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        return path.rsplit("/", 1)[-1] if path else url

    async def _post_with_retry(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self.client.request(
                    "POST",
                    url,
                    data=data,
                    headers=headers,
                )

                if response.status_code == 200:
                    return response

                if 500 <= response.status_code < 600:
                    last_error = RequestError(
                        f"Server error {response.status_code}",
                        url=url,
                        status_code=response.status_code,
                        attempts=attempt,
                    )
                else:
                    raise RequestError(
                        f"Request failed with status {response.status_code}",
                        url=url,
                        status_code=response.status_code,
                        attempts=attempt,
                    )

            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as e:
                last_error = e
            except RequestError as e:
                last_error = e
                if e.status_code is not None and 400 <= e.status_code < 500:
                    raise

            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))

        raise RequestError(
            f"Failed to fetch {url} after {self.max_retries} attempts",
            url=url,
            attempts=self.max_retries,
            cause=last_error,
        ) from last_error

    async def search(self, query: str) -> list[ComicInfo]:
        # 1) AJAX search endpoint
        api_url = f"{self.referer}/wp-admin/admin-ajax.php"
        try:
            response = await self._post_with_retry(
                api_url,
                data={
                    "action": "wp-manga-search-manga",
                    "title": query,
                },
            )
            data = response.json()
            if data.get("success") and isinstance(data.get("data"), list):
                results: list[ComicInfo] = []
                for item in data["data"]:
                    url = item.get("url", "")
                    label = item.get("label", "")
                    thumbnail = item.get("thumbnail", "")
                    if not url or not label:
                        continue

                    results.append(
                        ComicInfo(
                            title=label,
                            status="Unknown",
                            tags=[],
                            identifier=self._identifier_from_url(url),
                            thumbnail_url=thumbnail or "",
                            url=self._abs_url(url),
                            authors=[],
                            release_year="",
                            item_type="manga",
                        )
                    )

                if results:
                    return results
        except (ValueError, RequestError):
            pass

        # 2) HTML search fallback
        response = await self.safe_get(
            self.referer + "/",
            params={"s": query},
            headers={"Referer": f"{self.referer}/"},
        )

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        seen_urls: set[str] = set()

        for link in soup.find_all("a", href=re.compile(r"/serie/")):
            href = link.get("href")
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)

            title = link.get_text(strip=True)
            if not title:
                img = link.find("img")
                if img:
                    title = img.get("alt", "").strip()

            if not title:
                continue

            poster = None
            container = link.find_parent()
            if container:
                thumb = container.find_previous_sibling("div", class_="item-thumb")
                if thumb:
                    img = thumb.find("img")
                    if img:
                        poster = img.get("src")

            full_url = self._abs_url(href)
            results.append(
                ComicInfo(
                    title=title,
                    status="Unknown",
                    tags=[],
                    identifier=self._identifier_from_url(full_url),
                    thumbnail_url=poster or "",
                    url=full_url,
                    authors=[],
                    release_year="",
                    item_type="manga",
                )
            )

        return results

    async def get_chapter_list(self, item: ComicInfo) -> list[ComicChapter]:
        response = await self.safe_get(item.url)
        soup = BeautifulSoup(response.text, "html.parser")

        chapter_list = soup.find("ul", class_=["main", "version-chap", "no-volumn"])
        if not chapter_list:
            chapter_list = soup.find("ul", {"id": "chapter-list"})

        if not chapter_list:
            raise ParseError("Could not find chapter list")

        chapters: list[ComicChapter] = []

        for li in chapter_list.find_all("li", class_="wp-manga-chapter"):
            link = li.find("a")
            if not link:
                continue

            title = (link.get("title") or link.get_text(strip=True)).strip()
            href = link.get("href")
            if not title or not href:
                continue

            full_url = self._abs_url(href)

            chapter_id = self._identifier_from_url(full_url)
            if not chapter_id:
                chapter_id = title

            chapters.append(
                ComicChapter(
                    title=title,
                    url=full_url,
                    identifier=chapter_id,
                )
            )

        chapters.reverse()
        return chapters

    async def get_chapters_images(self, item: ComicChapter) -> list[str]:
        response = await self.safe_get(item.url)
        soup = BeautifulSoup(response.text, "html.parser")

        imgs = []
        reading_content = soup.find("div", class_="reading-content")
        if reading_content:
            imgs = reading_content.find_all("img", class_="wp-manga-chapter-img")
        else:
            imgs = soup.find_all("img", class_="wp-manga-chapter-img")

        if not imgs:
            container = soup.find("div", {"id": "chapter-images"})
            if container:
                for img_div in container.find_all("div", class_="chapter-image"):
                    img = img_div.find("img")
                    if img:
                        imgs.append(img)

        if not imgs:
            raise ParseError("Could not find chapter images")

        image_urls: list[str] = []
        seen: set[str] = set()

        for img in imgs:
            img_url = img.get("data-src") or img.get("src")
            if not img_url:
                continue

            full_url = self._abs_url(img_url)
            if full_url in seen:
                continue

            seen.add(full_url)
            image_urls.append(full_url)

        return image_urls