from __future__ import annotations

import csv
import re
import ssl
import time
from pathlib import Path
from urllib.parse import unquote

import requests
import scrapy
from deep_translator import GoogleTranslator

from mainz_sportstaetten_scraper.items import SportDetailItem


class SportDetailsSpider(scrapy.Spider):
    name = "sport_details"
    allowed_domains = ["www.mainz.de"]
    GEONODE_DNS = "proxy.geonode.io:9000"
    GEONODE_USERNAME = "crawlmagic"
    GEONODE_PASSWORD = "e6e10ef0-41fa-4b5d-9288-3902c239347f"

    custom_settings = {
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "COOKIES_ENABLED": False,
        "FEEDS": {
            "sportstaetten_directory_data.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": [
                    "resource_id",
                    "url",
                    "title",
                    "category_label",
                    "district",
                    "venue_address",
                    "large_playing_fields",
                    "small_playing_fields",
                    "other_facilities",
                    "parking_spaces",
                    "dimensions_area",
                    "hall_division",
                    "flooring",
                    "changing_rooms",
                    "accessible_for_people_with_disabilities",
                    "bus_lines",
                    "predominant_sports",
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

    def __init__(self, input_csv: str = "sportstaetten_listings.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        proxy = self._build_geonode_proxy()
        self._translator_auto = self.get_translator_with_proxy(proxy, source="auto")
        self._translator_de = self.get_translator_with_proxy(proxy, source="de")
        self._seen_url_category = set()

    @classmethod
    def _build_geonode_proxy(cls) -> dict[str, str]:
        auth = f"{cls.GEONODE_USERNAME}:{cls.GEONODE_PASSWORD}@{cls.GEONODE_DNS}"
        return {
            "http": f"http://{auth}",
            "https": f"http://{auth}",
        }

    def get_translator_with_proxy(
        self,
        proxy: dict[str, str],
        source: str = "auto",
    ) -> GoogleTranslator:
        """
        Build a deep_translator GoogleTranslator using the provided proxy/session.
        Falls back to default translator if proxy session init fails.
        """
        try:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            session = requests.Session()
            session.verify = False
            return GoogleTranslator(
                source=source,
                target="en",
                proxies={
                    "http": proxy.get("http", ""),
                    "https": proxy.get("https", proxy.get("http", "")),
                },
                session=session,
            )
        except Exception as e:
            self.logger.warning(
                "Proxy translator init failed for source=%s, fallback direct: %s",
                source,
                e,
            )
            return GoogleTranslator(source=source, target="en")

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

        decoded = unquote(email_raw)

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
            norm = SportDetailsSpider._normalize_website_href(str(raw))
            if not norm:
                continue
            wlow = norm.lower()
            if "openstreetmap" in wlow or "www.rmv.de" in wlow:
                continue
            if SportDetailsSpider._is_social_url(norm):
                continue
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    @staticmethod
    def _extract_labeled_value(text_nodes: list[str], labels: tuple[str, ...]) -> str:
        """
        Extract value from lines like 'Anschrift: ...' with tolerant label matching.
        """
        if not text_nodes:
            return ""
        escaped = [re.escape(x) for x in labels if x]
        if not escaped:
            return ""
        pattern = re.compile(
            rf"(?:{'|'.join(escaped)})\s*:\s*(.+)",
            flags=re.I,
        )
        for raw in text_nodes:
            line = re.sub(r"\s+", " ", (raw or "")).strip()
            if not line:
                continue
            m = pattern.search(line)
            if m:
                return m.group(1).strip()
        return ""

    @staticmethod
    def _looks_german(text: str) -> bool:
        if not text:
            return False
        if re.search(r"[äöüÄÖÜß]", text):
            return True
        t = text.casefold()
        if re.search(
            r"(straße|strasse|belag|spielfeld|kunstrasen|hartplatz|turnhalle|ehrenhof)",
            t,
        ):
            return True
        # Word-boundary tokens only (avoid "ja" inside unrelated words, "nein" in "meine").
        if re.search(r"\b(ja|nein)\b", t):
            return True
        return any(
            re.search(rf"\b{re.escape(w)}\b", t)
            for w in (
                "stadtteil",
                "halle",
                "sporthalle",
                "gymnasium",
                "schule",
                "mainz",
                "platz",
            )
        )

    def _prefer_german_source(self, chunk: str) -> bool:
        """
        Google ``sl=auto`` mis-reads standalone German ``ja`` as non-German and
        returns English ``and``. Prefer ``de`` → ``en`` when the chunk is clearly
        German or is exactly ``ja`` / ``nein``.
        """
        c = chunk.strip()
        if not c:
            return False
        if re.fullmatch(r"(ja|nein)\.?", c, flags=re.I):
            return True
        return self._looks_german(chunk)

    def _translate_to_en(self, value: str) -> str:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return text
        max_chunk = 4500
        chunks: list[str] = []
        if len(text) <= max_chunk:
            chunks = [text]
        else:
            rest = text
            while rest:
                cut = rest.rfind(". ", 0, max_chunk)
                if cut < max_chunk // 2:
                    cut = rest.rfind(" ", 0, max_chunk)
                if cut < max_chunk // 2:
                    cut = min(len(rest), max_chunk)
                chunks.append(rest[:cut].strip())
                rest = rest[cut:].lstrip(" .")
        out_parts: list[str] = []
        for chunk in chunks:
            out_parts.append(self._translate_chunk_to_en(chunk))
        return " ".join(p for p in out_parts if p).strip()

    def _translate_chunk_to_en(self, chunk: str) -> str:
        last_err: Exception | None = None
        orig = chunk.strip()
        translators = (
            (self._translator_de, self._translator_auto)
            if self._prefer_german_source(chunk)
            else (self._translator_auto, self._translator_de)
        )
        for attempt in range(3):
            for translator in translators:
                try:
                    result = (translator.translate(chunk) or "").strip()
                    if not result:
                        continue
                    # Google ``sl=auto`` often maps German ``ja`` → English ``and``.
                    if orig.casefold() == "ja" and result.casefold() == "and":
                        continue
                    if result != chunk:
                        return result
                    if not self._looks_german(chunk):
                        return result
                except Exception as e:
                    last_err = e
            time.sleep(0.35 * (attempt + 1))
        if last_err:
            self.logger.debug("Translate gave up after retries: %s", last_err)
        if orig.casefold() == "ja":
            return "yes"
        if orig.casefold() == "nein":
            return "no"
        return chunk

    @staticmethod
    def _is_bad_translation_result(text: str) -> bool:
        if not text:
            return False
        t = text.strip().lower()
        bad_markers = (
            "error 500",
            "server error",
            "<!doctype html",
            "<html",
            "traceback",
            "exception",
        )
        return any(m in t for m in bad_markers)

    @classmethod
    def _normalize_translated_value(
        cls,
        field_name: str,
        translated: str,
        original: str,
    ) -> str:
        val = (translated or "").strip()
        src = (original or "").strip()
        if cls._is_bad_translation_result(val):
            return src

        low = val.casefold()
        if field_name in (
            "small_playing_fields",
            "accessible_for_people_with_disabilities",
        ) and low == "and":
            src_low = src.casefold()
            if src_low == "ja":
                return "yes"
            if src_low == "nein":
                return "no"
            return src or val

        replacements = {
            "doubt hall": "two-court sports hall",
            "zweid sports hall": "two-court sports hall",
            "dreifeldhalle": "three-court sports hall",
            "rasengittersteine": "grass grid pavers",
        }
        low_map = val.casefold()
        if low_map in replacements:
            return replacements[low_map]
        return val

    @staticmethod
    def _format_office_address_paragraphs(parts: list[str]) -> str:
        """Prefer the street / PLZ block; drop leading org lines when clearly separated."""
        if not parts:
            return ""
        last = parts[-1]
        if re.search(r"\b\d{5}\b", last):
            if len(parts) >= 2 and re.fullmatch(
                r"\d{5}\s+Mainz.*", last.strip(), flags=re.I
            ):
                prev = parts[-2]
                if not re.search(r"\b\d{5}\b", prev):
                    return f"{prev}, {last}"
            return last
        return ", ".join(parts)

    def _contact_office_address_from_response(self, response: scrapy.http.Response) -> str:
        """Postal block under Kontakt → Adresse (office), not the venue ``Anschrift``."""
        ps = response.xpath(
            '//div[contains(@class,"SP-Contact__content--address")]'
            '//div[contains(@class,"SP-Contact__location")]//p'
        )
        parts: list[str] = []
        for p in ps:
            t = self._clean(p.xpath(".//text()").getall())
            if t:
                parts.append(t)
        if parts:
            return self._format_office_address_paragraphs(parts)
        return ""

    def _venue_body_lines(self, response: scrapy.http.Response) -> list[str]:
        """
        Full ``<p>`` / ``<li>`` lines from the article body before the Kontakt /
        SP-Contact block (so ``Anschrift`` is one string, not split text nodes).
        """
        nodes = response.xpath(
            '//article[contains(@id,"SP-Content")]'
            '//section[contains(@class,"SP-Contact__wrapper")]'
            '/preceding::*[self::p or self::li]'
            '[ancestor::article[contains(@id,"SP-Content")]]'
        )
        out: list[str] = []
        for node in nodes:
            t = self._clean(node.xpath(".//text()").getall())
            if t:
                out.append(t)
        if out:
            return out
        for node in response.xpath(
            '//article[contains(@id,"SP-Content")]//p | '
            '//article[contains(@id,"SP-Content")]//li'
        ):
            t = self._clean(node.xpath(".//text()").getall())
            if t:
                out.append(t)
        return out

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

    def parse_detail(self, response):
        row = response.meta["listing_row"]
        title = self._clean(
            response.xpath(
                '//article[contains(@id,"SP-Content")]//h1//text()'
            ).getall()
        )
        if not title:
            title = self._clean(
                response.xpath('//meta[@property="og:title"]/@content').getall()
            )
        text_nodes = response.xpath(
            '//article[contains(@id,"SP-Content")]//text()'
        ).getall()
        pre_contact_text_nodes = response.xpath(
            '//article[contains(@id,"SP-Content")]'
            '//h2[contains(translate(normalize-space(.),'
            '"ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÜ","abcdefghijklmnopqrstuvwxyzäöü"),"kontakt")]'
            '/preceding::*[self::p or self::li]//text()'
        ).getall()
        if not pre_contact_text_nodes:
            pre_contact_text_nodes = text_nodes
        paragraph_lines = [
            self._clean(p.xpath(".//text()").getall())
            for p in response.xpath('//article[contains(@id,"SP-Content")]//p')
        ]
        paragraph_lines = [x for x in paragraph_lines if x]
        pre_contact_lines = [
            re.sub(r"\s+", " ", (x or "")).strip() for x in pre_contact_text_nodes
        ]
        pre_contact_lines = [x for x in pre_contact_lines if x]
        labeled_lines = pre_contact_lines + paragraph_lines
        venue_body_lines = self._venue_body_lines(response)

        district = self._extract_labeled_value(
            labeled_lines,
            ("Stadtteil", "District"),
        )
        # Facility location (Anschrift): full lines from the body before Kontakt,
        # not the office postal block under Kontakt → Adresse.
        venue_address = self._extract_labeled_value(
            venue_body_lines,
            ("Anschrift", "Adresse", "Address"),
        )
        large_playing_fields = self._extract_labeled_value(
            labeled_lines,
            ("Großspielfelder", "Grossspielfelder", "Large playing fields"),
        )
        small_playing_fields = self._extract_labeled_value(
            labeled_lines,
            ("Kleinspielfelder", "Small playing fields"),
        )
        other_facilities = self._extract_labeled_value(
            labeled_lines,
            ("Sonstiges", "Other"),
        )
        parking_spaces = self._extract_labeled_value(
            labeled_lines,
            ("Parkplätze", "Parkplaetze", "Parking spaces", "Parking"),
        )
        dimensions_area = self._extract_labeled_value(
            labeled_lines,
            ("Abmessung/Fläche", "Dimensions/Area", "Dimensions/area"),
        )
        hall_division = self._extract_labeled_value(
            labeled_lines,
            ("Hallenteilung", "Hall division"),
        )
        flooring = self._extract_labeled_value(
            labeled_lines,
            ("Bodenbelag", "Flooring", "Floor covering"),
        )
        changing_rooms = self._extract_labeled_value(
            labeled_lines,
            ("Umkleidekabinen", "Changing rooms"),
        )
        accessible_for_people_with_disabilities = self._extract_labeled_value(
            labeled_lines,
            (
                "Behindertengerecht",
                "Accessible for people with disabilities",
                "Handicapped accessible",
            ),
        )
        # On some pages (e.g. Grundschule Finthen), mixed text-node extraction can
        # truncate this to "56,". Prefer full paragraph lines first, then fallback.
        bus_lines = self._extract_labeled_value(
            paragraph_lines,
            ("Buslinien", "Bus lines"),
        ) or self._extract_labeled_value(
            labeled_lines,
            ("Buslinien", "Bus lines"),
        )
        predominant_sports = self._extract_labeled_value(
            labeled_lines,
            ("Überwiegende Sportarten", "Predominant sports", "Main sports"),
        )

        # Leave description empty: unstructured fallbacks often duplicated the whole page.
        description = ""

        # Office / correspondence address: Kontakt → Adresse (SP-Contact__location).
        # ``venue_address`` is the facility ``Anschrift`` only.
        address = self._contact_office_address_from_response(response)
        if not address:
            address_nodes = response.xpath(
                '//div[contains(@class,"SP-Contact__location")]//p'
            )
            parts2: list[str] = []
            for p in address_nodes:
                t = self._clean(p.xpath(".//text()").getall())
                if t:
                    parts2.append(t)
            if parts2:
                address = self._format_office_address_paragraphs(parts2)

        tel_nodes = response.xpath(
            '//a[starts-with(@href,"tel:")]/div/span[@class="SP-Link__title"]/text()'
        ).getall()
        mobile = self._dedupe_join(tel_nodes)

        if not mobile:
            raw_text = " ".join(response.xpath("//p//text()").getall())

            matches = re.findall(
                r"(?:Phone|Telefon|Tel|Mobil|Mobile):\s*([\+\d\s\-\/]+)",
                raw_text,
                flags=re.I,
            )

            if matches:
                mobile = self._dedupe_join(list(set([m.strip() for m in matches])))

        if not mobile:
            mobile = (row.get("phone") or "").strip()

        fax = None
        website = []

        fax_href = response.xpath(
            '//h2[contains(translate(text(),"FAX","fax"),"fax")]/parent::div/following-sibling::ul/li[2]//a/@href'
        ).get()

        fax_text_nodes = response.xpath(
            '//h2[contains(translate(text(),"FAX","fax"),"fax")]/parent::div/following-sibling::ul/li[2]//*[@class="SP-Link__title"]/text()'
        ).getall()

        fax_text = self._dedupe_join(fax_text_nodes)

        if not fax_href and not fax_text:
            fax_text_nodes = response.xpath(
                '//li[contains(@class,"SP-Contact__links__item")][.//svg[contains(@class,"SPi-printer")]]//*[@class="SP-Link__title"]/text()'
            ).getall()
            fax_text = self._dedupe_join(fax_text_nodes)

        def is_phone_number(val):
            if not val:
                return False

            val = val.strip().lower()

            if "http" in val or "www" in val:
                return False

            return bool(re.search(r"\d{3,}", val))

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

        elif fax_text:
            if is_phone_number(fax_text):
                fax = fax_text
            else:
                norm = self._normalize_website_href(fax_text)
                if norm:
                    website.append(norm)

        fax = fax or ""

        if not fax:
            raw_text = " ".join(response.xpath("//p//text()").getall())

            match = re.search(
                r"(?:Fax|Telefax):\s*([\+\d\s\-\/]+)", raw_text, flags=re.I
            )
            if match:
                fax = match.group(1).strip()

        website = self._filter_website_list(website)

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
        if not website:
            content_links = response.xpath(
                '//article[contains(@id,"SP-Content")]//a/@href'
            ).getall()
            for link in content_links:
                norm = self._normalize_website_href(link or "")
                if not norm:
                    continue
                l = norm.lower()
                if "openstreetmap" in l or "www.rmv.de" in l:
                    continue
                if self._is_social_url(norm):
                    continue
                if "mainz.de/verzeichnisse/sportstaetten/" in l:
                    continue
                if norm not in website:
                    website.append(norm)
        email = (row.get("email") or "").strip()

        if not email:
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
            email = candidates[0] if candidates else ""

        email = self.clean_email(email)
        if isinstance(email, str) and email.count("@") > 1:
            local = email.split("@", 1)[0].strip()
            domain = email.rsplit("@", 1)[-1].strip()
            if local and domain:
                email = f"{local}@{domain}"
        email = email.replace("-de", ".de").replace("-en", ".en")

        way_us = []

        way_links = response.xpath(
            '//h2[contains(translate(text(),"WAY TO US","way to us"),"way to us")]'
            '/parent::div/following-sibling::*//a/@href'
        ).getall()

        if not way_links:
            way_links = response.xpath(
                '//h2[contains(translate(text(),"WEG ZU UNS","weg zu uns"),"weg zu uns")]'
                '/parent::div/following-sibling::*//a/@href'
            ).getall()

        for link in way_links:
            norm_way = self._normalize_website_href(link or "")
            if not norm_way:
                continue
            l = norm_way.lower()

            if "openstreetmap" in l:
                continue
            if self._is_social_url(norm_way):
                continue

            if norm_way not in way_us:
                way_us.append(norm_way)
        category_label = (row.get("filter_fil_2") or "").strip()

        title_src = title
        category_label_src = category_label
        district_src = district
        venue_address_src = venue_address
        large_playing_fields_src = large_playing_fields
        small_playing_fields_src = small_playing_fields
        other_facilities_src = other_facilities
        parking_spaces_src = parking_spaces
        dimensions_area_src = dimensions_area
        hall_division_src = hall_division
        flooring_src = flooring
        changing_rooms_src = changing_rooms
        accessible_src = accessible_for_people_with_disabilities
        bus_lines_src = bus_lines
        predominant_sports_src = predominant_sports
        address_src = address

        title = self._normalize_translated_value(
            "title", self._translate_to_en(title_src), title_src
        )
        category_label = self._normalize_translated_value(
            "category_label",
            self._translate_to_en(category_label_src),
            category_label_src,
        )
        district = self._normalize_translated_value(
            "district", self._translate_to_en(district_src), district_src
        )
        venue_address = self._normalize_translated_value(
            "venue_address", self._translate_to_en(venue_address_src), venue_address_src
        )
        large_playing_fields = self._normalize_translated_value(
            "large_playing_fields",
            self._translate_to_en(large_playing_fields_src),
            large_playing_fields_src,
        )
        small_playing_fields = self._normalize_translated_value(
            "small_playing_fields",
            self._translate_to_en(small_playing_fields_src),
            small_playing_fields_src,
        )
        other_facilities = self._normalize_translated_value(
            "other_facilities",
            self._translate_to_en(other_facilities_src),
            other_facilities_src,
        )
        parking_spaces = self._normalize_translated_value(
            "parking_spaces",
            self._translate_to_en(parking_spaces_src),
            parking_spaces_src,
        )
        dimensions_area = self._normalize_translated_value(
            "dimensions_area",
            self._translate_to_en(dimensions_area_src),
            dimensions_area_src,
        )
        hall_division = self._normalize_translated_value(
            "hall_division",
            self._translate_to_en(hall_division_src),
            hall_division_src,
        )
        flooring = self._normalize_translated_value(
            "flooring", self._translate_to_en(flooring_src), flooring_src
        )
        changing_rooms = self._normalize_translated_value(
            "changing_rooms",
            self._translate_to_en(changing_rooms_src),
            changing_rooms_src,
        )
        accessible_for_people_with_disabilities = self._normalize_translated_value(
            "accessible_for_people_with_disabilities",
            self._translate_to_en(accessible_src),
            accessible_src,
        )
        bus_lines = self._normalize_translated_value(
            "bus_lines", self._translate_to_en(bus_lines_src), bus_lines_src
        )
        predominant_sports = self._normalize_translated_value(
            "predominant_sports",
            self._translate_to_en(predominant_sports_src),
            predominant_sports_src,
        )
        address = self._normalize_translated_value(
            "address", self._translate_to_en(address_src), address_src
        )

        item = SportDetailItem()
        item["resource_id"] = (row.get("resource_id") or "").strip()
        item["url"] = response.url
        item["title"] = title
        item["category_label"] = category_label
        item["district"] = district
        item["venue_address"] = venue_address
        item["large_playing_fields"] = large_playing_fields
        item["small_playing_fields"] = small_playing_fields
        item["other_facilities"] = other_facilities
        item["parking_spaces"] = parking_spaces
        item["dimensions_area"] = dimensions_area
        item["hall_division"] = hall_division
        item["flooring"] = flooring
        item["changing_rooms"] = changing_rooms
        item["accessible_for_people_with_disabilities"] = (
            accessible_for_people_with_disabilities
        )
        item["bus_lines"] = bus_lines
        item["predominant_sports"] = predominant_sports
        item["description"] = description
        item["address"] = address
        item["mobile"] = mobile
        item["fax"] = fax
        item["email"] = email
        item["website"] = website
        item["way_us"] = way_us

        dedupe_key = (
            (item.get("url") or "").strip().rstrip("/").lower(),
            (item.get("category_label") or "").strip().lower(),
        )
        if dedupe_key in self._seen_url_category:
            self.logger.debug(
                "Skipping duplicate item for url/category_label: %s | %s",
                dedupe_key[0],
                dedupe_key[1],
            )
            return

        self._seen_url_category.add(dedupe_key)
        yield item
