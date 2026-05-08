from __future__ import annotations
import csv
import re
from pathlib import Path
import scrapy
from urllib.parse import unquote
from deep_translator import GoogleTranslator
from mainz_adress_scraper.items import AdressDetailItem


class AdressDetailsSpider(scrapy.Spider):
    name = "adress_details"
    allowed_domains = ["www.mainz.de"]

    custom_settings = {
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "COOKIES_ENABLED": False,
        "FEEDS": {
            "address_directory_data_17_04_2026.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": [
                    "resource_id",
                    "url",
                    "search_text",
                    "title",
                    "category_label",
                    "description",
                    "address",
                    "mobile",
                    "fax",
                    "email",
                    "website",
                    "way_us",
                ],
            }
        },
    }

    def __init__(self, input_csv: str = "mainz_listings.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        # Initialize translator (auto-detect source language -> English)
        self.translator = GoogleTranslator(source="auto", target="en")
        # Track emitted rows; listing ``search_text`` is part of the dedupe key so the
        # same URL+category from different listing queries stays distinct.
        self._seen_url_category_search = set()

    # -------------------------------
    # Helpers
    # -------------------------------
    @staticmethod
    def _clean(parts):
        if parts is None:
            return ""
        if isinstance(parts, str):
            parts = [parts]
        out = []
        for p in parts:
            t = re.sub(r"\s+", " ", (p or "")).strip()
            if t:
                out.append(t)
        return " ".join(out).strip()

    @staticmethod
    def _dedupe_join(values):
        seen = set()
        out = []
        for v in values:
            vv = re.sub(r"\s+", " ", (v or "")).strip()
            if vv and vv not in seen:
                seen.add(vv)
                out.append(vv)
        return ";".join(out)

    @staticmethod
    def clean_email(email_raw: str) -> str:
        if not email_raw:
            return ""

        # Step 1: URL decode
        decoded = unquote(email_raw)

        # Step 2: Replace obfuscated characters
        decoded = decoded.replace("⚹", "@").replace("◦", ".")

        return decoded.strip()

    @staticmethod
    def _normalize_website_href(href: str) -> str | None:
        """Return a usable http(s) URL, or None (never mailto/tel/fax or bare email)."""
        if not href:
            return None
        h = href.strip()
        low = h.lower()
        if low.startswith("mailto:") or low.startswith("tel:") or low.startswith("fax:"):
            return None
        if "@" in h and not low.startswith("http"):
            return None
        if low.startswith("http://") or low.startswith("https://"):
            return h
        if low.startswith("www."):
            return "https://" + h
        return None

    @staticmethod
    def _is_social_url(url: str) -> bool:
        low = (url or "").lower()
        social_keywords = (
            "instagram",
            "facebook",
            "twitter",
            "x.com",
            "linkedin",
            "youtube",
        )
        return any(k in low for k in social_keywords)

    @staticmethod
    def _filter_website_list(links: list) -> list:
        out = []
        seen = set()
        for raw in links or []:
            norm = AdressDetailsSpider._normalize_website_href(str(raw))
            if not norm:
                continue
            wlow = norm.lower()
            if "openstreetmap" in wlow or "www.rmv.de" in wlow:
                continue
            if AdressDetailsSpider._is_social_url(norm):
                continue
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def _translate_to_en(self, value: str) -> str:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return text
        try:
            return self.translator.translate(text)
        except Exception:
            # Keep original text if translation service fails for any row.
            return text



    # -------------------------------
    # Start Requests
    # -------------------------------
    def start_requests(self):
        csv_path = Path(self.input_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path

        if not csv_path.exists():
            self.logger.error("Input CSV not found: %s", csv_path)
            return

        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = (row.get("detail_url") or "").strip()
                if not url:
                    continue

                yield scrapy.Request(
                    url=url,
                    callback=self.parse_detail,
                    dont_filter=True,
                    meta={"listing_row": row},
                )

    # -------------------------------
    # Parse Detail Page
    # -------------------------------
    def parse_detail(self, response):
        row = response.meta["listing_row"]
        title = response.xpath('//meta[@property="og:title"]/@content').getall()
        title = self._clean(title)

        # -------------------------------
        # Description
        # -------------------------------
        desc_nodes = response.xpath(
            '//h2[contains(@id,"description")]/parent::div/following-sibling::div//text()'
        ).getall()
        if not desc_nodes:
            desc_nodes = response.xpath(
                '//strong[contains(text(),"Sponsors")]/parent::p/following-sibling::p[1]/text()'
            ).getall()
        if not desc_nodes:
            desc_nodes = response.xpath(
                '//article[contains(@id,"SP-Content")]//div[contains(@class,"SP-Text SP")]//div[@class="SP-Paragraph"]//p/text() | //article[contains(@id,"SP-Content")]//div[contains(@class,"SP-Text SP")]//div[@class="SP-Paragraph"]/ul/li//text()'
            ).getall()

        description = self._clean(desc_nodes)

        # -------------------------------
        # Full Address (name + street)
        # -------------------------------
        address_nodes = response.xpath(
            '//div[contains(@class,"SP-Contact__location")]//p/text()'
        ).getall()


        address = self._clean(address_nodes)

        if not address:

            # 🎯 target contact-like blocks only
            raw_nodes = response.xpath(
                '//p[.//strong[contains(text(),"Contact")]]//text() | '
                '//p[contains(text(),"Fax") or contains(text(),"Phone")]//text()'
            ).getall()

            raw_text = " ".join([t.strip() for t in raw_nodes if t.strip()])

            # ✂️ remove unwanted parts
            clean = re.split(r'Fax:|Phone:|Tel:|E-mail:|Email:', raw_text)[0]

            # 🧹 cleanup
            clean = re.sub(r'\s+,', ',', clean)
            clean = re.sub(r',\s*,', ',', clean)
            clean = re.sub(r'\s{2,}', ' ', clean)

            address = self._clean(clean.strip())

        

        # -------------------------------
        # Telephone → mobile
        # -------------------------------
        tel_nodes = response.xpath(
            '//a[starts-with(@href,"tel:")]/div/span[@class="SP-Link__title"]/text()'
        ).getall()
        mobile = self._dedupe_join(tel_nodes)
        

        if not mobile:

            raw_text = " ".join(response.xpath('//p//text()').getall())

            matches = re.findall(r'Phone:\s*([\+\d\s\-\/]+)', raw_text)

            if matches:
                mobile = list(set([m.strip() for m in matches]))

        if not mobile:
            mobile = (row.get("phone") or "").strip()
        
        # -------------------------------
        # Fax + Website (correct logic)
        # -------------------------------

        fax = None
        website = []

        # 1️⃣ Try href
        fax_href = response.xpath(
            '//h2[contains(translate(text(),"FAX","fax"),"fax")]/parent::div/following-sibling::ul/li[2]//a/@href'
        ).get()

        # 2️⃣ Try text fallback
        fax_text_nodes = response.xpath(
            '//h2[contains(translate(text(),"FAX","fax"),"fax")]/parent::div/following-sibling::ul/li[2]//*[@class="SP-Link__title"]/text()'
        ).getall()

        fax_text = self._dedupe_join(fax_text_nodes)

        # 3️⃣ SVG printer fallback
        if not fax_href and not fax_text:
            fax_text_nodes = response.xpath(
                '//li[contains(@class,"SP-Contact__links__item")][.//svg[contains(@class,"SPi-printer")]]//*[@class="SP-Link__title"]/text()'
            ).getall()
            fax_text = self._dedupe_join(fax_text_nodes)

        # -------------------------------
        # Decide fax vs website
        # -------------------------------

        def is_phone_number(val):
            if not val:
                return False

            val = val.strip().lower()

            if "http" in val or "www" in val:
                return False

            return bool(re.search(r"\d{3,}", val)) # basic number check

        # Prefer href (li[2] is sometimes mailto: — must not go into website)
        if fax_href:
            fh = fax_href.strip()
            low = fh.lower()
            if low.startswith("mailto:"):
                pass
            elif low.startswith("tel:") or low.startswith("fax:"):
                cleaned = fh.replace("tel:", "").replace("fax:", "").strip()
                if is_phone_number(cleaned):
                    fax = cleaned
            else:
                norm = self._normalize_website_href(fh)
                if norm:
                    website.append(norm)

        # Else check text
        elif fax_text:
            if is_phone_number(fax_text):
                fax = fax_text
            else:
                norm = self._normalize_website_href(fax_text)
                if norm:
                    website.append(norm)

        fax = fax or ""

        if not fax:

            raw_text = " ".join(response.xpath('//p//text()').getall())

            match = re.search(r'Fax:\s*([\+\d\s\-\/]+)', raw_text)
            if match:
                fax = match.group(1).strip()


        website = self._filter_website_list(website)
        # -------------------------------
        # Website (separate extraction)
        # -------------------------------

        if not website:
            site_links = response.xpath(
                '//li[contains(@class,"SP-Contact__links__item")]//a/@href'
            ).getall()

            for link in site_links:
                norm = self._normalize_website_href(link or "")
                if not norm:
                    continue
                l = norm.lower()
                if "openstreetmap" in l or "www.rmv.de" in l:
                    continue
                if self._is_social_url(norm):
                    continue
                if norm not in website:
                    website.append(norm)
        # -------------------------------
        # Email (CSV → fallback to site)
        # -------------------------------
        email = (row.get("email") or "").strip()

        if not email:
            # Prefer the visible email text if present (often already "de-obfuscated").
            email_texts = response.xpath(
                '//a[contains(@href,"mailto:")]//text()'
            ).getall()
            email_text = self._clean(email_texts)
            if email_text and "@" in email_text and " " not in email_text:
                email = email_text

        if not email:
            email_raw = response.xpath('//a[@data-sp-email]/@href').get()
            if email_raw and "mailto:" in email_raw:
                email = email_raw.replace("mailto:", "").strip()

        if not email:
            mailto_hrefs = response.xpath('//a[contains(@href,"mailto:")]/@href').getall()
            candidates = []
            for href in mailto_hrefs:
                if not href:
                    continue
                clean = href.replace("mailto:", "").strip()
                if clean:
                    candidates.append(clean)
            # If there are multiple, keep the first after cleaning.
            email = candidates[0] if candidates else ""

        email = self.clean_email(email)
        # Some pages intentionally produce strings with multiple "@" after de-obfuscation,
        # e.g. "info@keim.nfo@keim24-de". Salvage those by keeping the first local-part
        # and the last domain-like part.
        if isinstance(email, str) and email.count("@") > 1:
            local = email.split("@", 1)[0].strip()
            domain = email.rsplit("@", 1)[-1].strip()
            if local and domain:
                email = f"{local}@{domain}"
        email = email.replace("-de", ".de").replace("-en", ".en")

        # -------------------------------
        # Way to us (fixed)
        # -------------------------------

        way_us = []

        way_links = response.xpath(
            '//h2[contains(translate(text(),"WAY TO US","way to us"),"way to us")]'
            '/parent::div/following-sibling::*//a/@href'
        ).getall()

        for link in way_links:
            norm_way = self._normalize_website_href(link or "")
            if not norm_way:
                continue
            l = norm_way.lower()

            # ❌ skip ONLY openstreetmap
            if "openstreetmap" in l:
                continue
            if self._is_social_url(norm_way):
                continue

            if norm_way not in way_us:
                way_us.append(norm_way)
        # -------------------------------
        # Build Item
        # -------------------------------
        category_label = (row.get("filter_fil_2") or "").strip()

        # Translate only requested string fields.
        title = self._translate_to_en(title)
        category_label = self._translate_to_en(category_label)
        description = self._translate_to_en(description)
        address = self._translate_to_en(address)

        item = AdressDetailItem()
        item["resource_id"] = (row.get("resource_id") or "").strip()
        item["url"] = response.url
        item["search_text"] = (row.get("search_text") or "").strip()
        item["title"] = title
        item["category_label"] = category_label
        item["description"] = description
        item["address"] = address
        item["mobile"] = mobile
        item["fax"] = fax
        item["email"] = email
        item["website"] = website
        item["way_us"] = way_us

        st = (row.get("search_text") or "").strip().lower()
        dedupe_key = (
            (item.get("url") or "").strip().rstrip("/").lower(),
            (item.get("category_label") or "").strip().lower(),
            st,
        )
        if dedupe_key in self._seen_url_category_search:
            self.logger.debug(
                "Skipping duplicate item for url/category_label/search_text: %s | %s | %s",
                dedupe_key[0],
                dedupe_key[1],
                dedupe_key[2],
            )
            return

        self._seen_url_category_search.add(dedupe_key)
        yield item
