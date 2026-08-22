from coco.provider import Provider, ComicInfo, ComicChapter
from bs4 import BeautifulSoup

class Weebcentral(Provider):
    name = "Weebcentral"
    code_name = "wc"
    referer = "https://weebcentral.com"

    def __init__(self, proxy = "", *, client = None, timeout = None, follow_redirects = True, headers = None):
        super().__init__(proxy, client=client, timeout=timeout, follow_redirects=follow_redirects, headers=headers)

    async def search(self, query: str) -> list[ComicInfo]:
        params = {
            'author': '',
            'text': query,
            'sort': 'Best Match',
            'order': 'Ascending',
            'official': 'Any',
            'display_mode': 'Full Display',
            'offset': 0
        }

        page = await self.safe_get(self.referer+"/search/data", params)
        soup = BeautifulSoup(page.text, "html.parser")

        results: list[ComicInfo] = []

        for article in soup.find_all("article", {"class": "bg-base-300"}):
            sections = article.find_all("section")

            link = sections[0].find("a").get("href")
            name = sections[1].find("div").find("a").string
            image_url = sections[0].find("a").find(
                "article").find("picture").find("img").get("src")
            manga_id = link.split("/")[4]
            year = sections[1].find_all("div")[1].find("span").string
            status = sections[1].find_all("div")[2].find("span").string
            type_ = sections[1].find_all("div")[3].find("span").string
            author = sections[1].find_all("div")[4].find("span").string
            tags = []
            for tag in sections[1].find_all("div")[5].find_all("span"):
                tags.append(tag.string.replace(",", ""))

            results.append(ComicInfo(
                title=name,
                status=status,
                tags=tags,
                identifier=manga_id,
                thumbnail_url=image_url,
                url=link,
                authors=[author],
                release_year=year,
                item_type=type_
            ))

        return results

    async def get_chapter_list(self, item: ComicInfo) -> list[ComicChapter]:
        url = f"{self.referer}/series/{item.identifier}/full-chapter-list"
        page = await self.safe_get(url)
        soup = BeautifulSoup(page.text, "html.parser")

        results: list[ComicChapter] = []
        for div in soup.find_all("div", {"class": "flex items-center"}):
            link = div.find("a").get("href")
            name = div.find("a").find_all("span")[1].find("span").string

            results.append(ComicChapter(
                title=name,
                url=link,
                identifier=link.split("/")[-1]
            ))

        results.reverse()
        return results


    async def get_chapters_images(self, item: ComicChapter) -> list[str]:
        url = f"{self.referer}/chapters/{item.identifier}/images?is_prev=False&current_page=1&reading_style=long_strip"
        page = await self.safe_get(url)
        soup = BeautifulSoup(page.text, "html.parser")

        image_urls: list[str] = []
        for img in soup.find("section").find_all("img"):
            image_urls.append(img.get("src"))

        return image_urls
