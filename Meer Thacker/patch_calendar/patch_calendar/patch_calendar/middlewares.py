from scrapy import signals


class PatchCalendarSpiderMiddleware:
    """Spider middleware — passes items/requests through unchanged."""

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        crawler.signals.connect(instance.spider_opened, signal=signals.spider_opened)
        return instance

    def process_spider_output(self, response, result, spider):
        yield from result

    async def process_start(self, start):
        async for item_or_request in start:
            yield item_or_request

    def spider_opened(self, spider):
        spider.logger.debug(f"Spider opened: {spider.name}")


class PatchCalendarDownloaderMiddleware:
    """Downloader middleware — passes requests/responses through unchanged."""

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        crawler.signals.connect(instance.spider_opened, signal=signals.spider_opened)
        return instance

    def process_request(self, request, spider):
        return None

    def process_response(self, request, response, spider):
        return response

    def spider_opened(self, spider):
        spider.logger.debug(f"Spider opened: {spider.name}")
