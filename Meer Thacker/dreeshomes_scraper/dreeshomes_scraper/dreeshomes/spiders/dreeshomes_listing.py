import re
import scrapy
from urllib.parse import urlparse


class DreeshomesListingSpider(scrapy.Spider):
    name = "dreeshomes_listing"
    allowed_domains = ["dreeshomes.com"]
    start_urls = ["https://www.dreeshomes.com/sitemap.xml"]
    url_markers = ("new-homes-",)
    disallowed_markers = (
        "/archived-communities/",
        "/dreesmart-innovations/",
        "/where-we-build/",
        "/design-center",
        "/blog/",
        "/why-drees/",
    )

    def parse(self, response):
        # Handle both sitemap indexes and direct urlset sitemap files.
        sitemap_links = response.xpath("//*[local-name()='sitemap']/*[local-name()='loc']/text()").getall()
        if sitemap_links:
            for sitemap_url in sitemap_links:
                yield response.follow(sitemap_url.strip(), callback=self.parse)
            return

        page_links = response.xpath("//*[local-name()='url']/*[local-name()='loc']/text()").getall()
        for raw_url in page_links:
            url = raw_url.strip()
            if not self.is_home_or_community_url(url):
                continue
            house_url = self._normalize_url(url)
            if not house_url:
                continue
            yield {
                "state": self._extract_state_from_url(house_url),
                "community_url": self._community_url_from_house_url(house_url),
                "house_url": house_url,
            }

    def is_home_or_community_url(self, url):
        url_lower = url.lower()
        if not any(marker in url_lower for marker in self.url_markers):
            return False
        # Never keep floorplan URLs under /new-homes-* sections.
        # Handles forms like "/floorplan/", "/floorplans/", "-floorplan/", etc.
        if re.search(r"floor-?plans?", url_lower):
            return False
        if any(marker in url_lower for marker in self.disallowed_markers):
            return False

        parts = [p for p in urlparse(url_lower).path.split("/") if p]
        if not parts:
            return False
        last_slug = parts[-1]
        # Keep only address-style leaves where slug starts with house number,
        # e.g. "3216-lookout-mountain-road".
        return bool(re.match(r"^\d+", last_slug))

    @staticmethod
    def _normalize_url(url):
        raw = str(url or "").strip()
        if not raw:
            return ""
        clean = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if not (clean.startswith("http://") or clean.startswith("https://")):
            return ""
        return clean

    @staticmethod
    def _extract_state_from_url(url):
        parts = [p for p in urlparse(url).path.split("/") if p]
        # Expected format: /new-homes-{market}/{city-state}/{community}/...
        if len(parts) < 2:
            return ""
        city_state = parts[1].lower()
        if "-" in city_state:
            return city_state.rsplit("-", 1)[-1].upper()
        return ""

    @staticmethod
    def _community_url_from_house_url(house_url):
        if not house_url:
            return ""
        # Keep community path: /new-homes-market/city-state/community
        parts = [p for p in urlparse(house_url).path.split("/") if p]
        if len(parts) < 3:
            return ""
        return f"https://www.dreeshomes.com/{parts[0]}/{parts[1]}/{parts[2]}"
