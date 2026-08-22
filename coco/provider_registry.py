from dataclasses import dataclass
import re
from typing import Pattern


@dataclass(slots=True, frozen=True)
class ProviderRegistry:
    title: str
    code_name: str
    base_url: str
    url_detection_regex: Pattern[str]
    is_nsfw: bool


provider_registry: dict[str, ProviderRegistry] = {
    "wc": ProviderRegistry(
        title="Weebcentral",
        code_name="wc",
        base_url="https://weebcentral.com",
        url_detection_regex=re.compile(r"^https?://(?:www\.)?weebcentral\.com(?:/|$)"),
        is_nsfw=False,
    ),
    "tl": ProviderRegistry(
        title="Toonily",
        code_name="tl",
        base_url="https://toonily.com",
        url_detection_regex=re.compile(r"^https?://(?:www\.)?toonily\.com(?:/|$)"),
        is_nsfw=True,
    ),
    "nh": ProviderRegistry(
        title="Nhentai",
        code_name="nh",
        base_url="https://nhentai.net",
        url_detection_regex=re.compile(r"^https?://(?:www\.)?nhentai\.net(?:/|$)"),
        is_nsfw=True,
    ),
}