from coco.provider import Provider, ComicChapter, ComicInfo, ProviderError


class Nhentai(Provider):
    def __init__(self, proxy = "", *, client = None, timeout = None, follow_redirects = True, headers = None):
        super().__init__(proxy, client=client, timeout=timeout, follow_redirects=follow_redirects, headers=headers)
        self.base_url = "https://nhentai.net/api/v2"

    async def get_cdn(self) -> dict[str, str]:
        url = self.base_url + "/cdn"
        page = await self.safe_get(url)
        return page.json()

    async def search(self, query: str, sort: str = "popular", page: int=1) -> list[ComicInfo]:
        url = self.base_url + "/search"
        params = {
            "query": query,
            "sort": sort,
            "page": page
        }

        response = await self.safe_get(url, params=params)
        cdns = await self.get_cdn()
        if not page:
            return []

        match response.status_code:
            case 422:
                raise ProviderError("Nhentai: Validation Error")
            case 429:
                raise ProviderError("Nhentai: too many requests, try again later")

        results = []
        for result in response.json()["result"]:
            results.append(ComicInfo(
                title=result["english_title"],
                thumbnail_url=cdns["thumb_servers"][0] + result["thumbnail"],
                identifier=result["id"],
                tags=result["tag_ids"],
                url="https://nhentai.net/g/"+ str(result["id"]),
                authors=[],
                release_year=0,
                item_type="doujin",
                status="published"
            ))

        return results


    async def get_chapter_list(self, item: ComicInfo) -> list[ComicChapter]:
        # there is no such thing as chapters in nhentai and all content is a single chapter kind, but for sake of following the abstractions, we will do this
        return [ComicChapter(
            title=item.title,
            url=item.url,
            identifier=item.identifier,
        )]

    async def get_chapters_images(self, item: ComicChapter) -> list[str]:
        url = self.base_url + f"/galleries/{item.identifier}"
        cdn = await self.get_cdn()
        response = await self.safe_get(url)

        match response.status_code:
            case 422:
                raise ProviderError("Nhentai: Validation Error")
            case 429:
                raise ProviderError("Nhentai: too many requests, try again later")
            case 404:
                raise ProviderError("Nhentai: not found")

        results = []
        for image in response.json()["pages"]:
            results.append(cdn["image_servers"][0] + "/" + image["path"])
        return results
