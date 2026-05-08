import csv
import html
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import scrapy


class KhovProductionSpider(scrapy.Spider):
    name = "khov_production"
    allowed_domains = ["khov.com", "www.khov.com"]
    REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
    # Canonical listing buckets (gallery.<key>[].url when the site exposes listing.gallery).
    _LISTING_GALLERY_KEYS: Tuple[str, str, str, str] = (
        "exterior",
        "interiorFurnished",
        "interiorUnfurnished",
        "construction",
    )
    # QMI / RSC pages often omit listing.gallery; the same four buckets appear only under these keys.
    _HERO_GALLERY_KEYS: Tuple[Tuple[str, str], ...] = (
        ("exterior", "HeroGalleryExteriorImages"),
        ("interiorFurnished", "HeroGalleryInteriorFurnishedImages"),
        ("interiorUnfurnished", "HeroGalleryInteriorUnfurnishedImages"),
        ("construction", "HeroGalleryConstructionImages"),
    )
    _NORMAL_ONLY_MODEL_FIELDS: Tuple[str, ...] = (
        "Model Name",
        "# BR",
        "# BA",
        "# 1/2 BA",
        "Minimum SQFT",
        "Maximum SQFT",
        "Minimum Base Price (Current)",
    )
    _QMI_ONLY_FIELDS: Tuple[str, ...] = (
        "QMI Model Name",
        "QMI # of BR",
        "QMI # of BA",
        "QMI # of 1/2 BA",
        "QMI Model SQFT",
        "QMI Model Price",
        "QMI Availability Date",
        "QMI Lot SQFT",
        "QMI Incentives",
    )

    FIELDNAMES: List[str] = [
        "url",
        "Community Details",
        "Community Name",
        "latitude",
        "longitude",
        "Street Address",
        "City",
        "State",
        "Zip Code",
        "County",
        "Model hours",
        "Phone number",
        "Website",
        "status",
        "Builder",
        "Product Type (SFD/SFA/CO)",
        "Model/Product Types Available",
        "Avg Lot Size",
        "Avg Lot - Width/Depth",
        "Garages (Y/N)",
        "# of Garages",
        "Adult Community (Y/N)",
        "Amenities Available",
        "Amenity Type",
        "Attributes/Features",
        "Overall Description of the Community",
        "Foundation Type",
        "Exterior Specifications Available",
        "Interior Specifications Available",
        "HOA Fee",
        "HOA Services",
        "Other Fees (i.e. CDD)",
        "City/Town/Property Tax %",
        "Sales Start Date",
        "Sold Out Date",
        "Total Lots Sold",
        "Incentives",
        "QMI Incentives",
        "QMI Model Name",
        "QMI # of Garages",
        "QMI # of BR",
        "QMI # of BA",
        "QMI # of 1/2 BA",
        "QMI Model SQFT",
        "QMI Model Price",
        "QMI Availability Date",
        "QMI Lot SQFT",
        "QMI Interior/Exterior Attributes",
        "Model Details",
        "Model Name",
        "Plan Description",
        "# BR",
        "# BA",
        "# 1/2 BA",
        "# of Floors",
        "Parking Type",
        "Plan Garage Entry (Front load)",
        "# Garages",
        "Minimum SQFT",
        "Maximum SQFT",
        "Minimum Base Price (Current)",
        "Maximum Base Price/All In Price",
        "Previous Price",
        "Last Updated",
        "Plan Features",
        "Interior Specifications Descriptions",
        "First Floor Master (Y/N)",
        "images",
    ]

    custom_settings = {
        "FEEDS": {
            "khov_data.csv": {
                "format": "csv",
                "overwrite": True,
                "encoding": "utf-8",
                "fields": FIELDNAMES,
            }
        },
        "FEED_EXPORT_ENCODING": "utf-8",
        "LOG_LEVEL": "INFO",
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_DELAY": 0.25,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.5,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "RETRY_TIMES": 4,
        # Use explicit public DNS resolvers to reduce environment-specific lookup issues.
        "DNS_RESOLVER": "scrapy.resolver.CachingThreadedResolver",
        "DNS_SERVERS": ["1.1.1.1", "8.8.8.8"],
        "DNS_TIMEOUT": 20,
        # khov.com occasionally serves Content-Length mismatches; avoid failing the crawl.
        "DOWNLOAD_FAIL_ON_DATALOSS": False,
        # Never follow HTTP redirects (meta dont_redirect is not always honored by other stacks).
        "REDIRECT_ENABLED": False,
    }

    def __init__(
        self,
        urls_csv: str = "khov_urls.csv",
        max_urls: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.urls_csv = urls_csv
        self.max_urls = int(max_urls) if max_urls not in (None, "", "0") else None
        self.scraped_count = 0

    def start_requests(self) -> Iterable[scrapy.Request]:
        path = Path(self.urls_csv)
        if not path.is_absolute():
            # default: project root (same folder where you run `scrapy crawl`)
            path = Path.cwd() / path
            if not path.exists():
                # also try one directory above (common when running from repo root)
                alt = Path.cwd().parent / self.urls_csv
                if alt.exists():
                    path = alt

        if not path.exists():
            raise FileNotFoundError(f"URLs CSV not found: {path}")

        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return

            url_key = "listing_url" if "listing_url" in reader.fieldnames else reader.fieldnames[0]
            queued = 0
            for row in reader:
                if self.max_urls is not None and queued >= self.max_urls:
                    break

                url = (row.get(url_key) or "").strip()
                if not url:
                    continue

                queued += 1
                yield scrapy.Request(
                    url=url,
                    callback=self.parse,
                    errback=self.errback,
                    dont_filter=True,
                    meta={
                        "expected_fetch_url": url,
                        "tried_alt_host": False,
                        "dont_redirect": True,
                        "handle_httpstatus_list": sorted(self.REDIRECT_STATUS_CODES),
                    },
                )

    def _swap_khov_host(self, url: str) -> str:
        if re.search(r"^https?://www\.khov\.com\b", url, flags=re.I):
            return re.sub(r"^https?://www\.khov\.com\b", "https://khov.com", url, flags=re.I)
        if re.search(r"^https?://khov\.com\b", url, flags=re.I):
            return re.sub(r"^https?://khov\.com\b", "https://www.khov.com", url, flags=re.I)
        return url

    def errback(self, failure: Any) -> Any:
        req = failure.request
        message = str(failure.value)
        if "DNS lookup failed" in message and not req.meta.get("tried_alt_host"):
            alt_url = self._swap_khov_host(getattr(req, "url", ""))
            if alt_url and alt_url != getattr(req, "url", ""):
                self.logger.warning("DNS failed for %s; retrying with alternate host %s", req.url, alt_url)
                meta = dict(req.meta)
                meta["tried_alt_host"] = True
                return scrapy.Request(
                    url=alt_url,
                    callback=self.parse,
                    errback=self.errback,
                    dont_filter=True,
                    meta=meta,
                )
        self.logger.warning("Request failed (%s): %s", getattr(req, "url", "?"), failure.value)

    def _listing_url_identity(self, url: str) -> str:
        """Normalize URL so CSV listing URL can be compared to the final response URL."""
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        scheme = (p.scheme or "https").lower()
        if scheme == "http":
            scheme = "https"
        path = (p.path or "").rstrip("/")
        if not path:
            path = ""
        q = p.query
        return f"{scheme}://{host}{path}?{q}" if q else f"{scheme}://{host}{path}"

    def _response_matches_expected_fetch(self, response: scrapy.http.Response) -> bool:
        expected = response.request.meta.get("expected_fetch_url")
        if not expected or not isinstance(expected, str):
            return True
        return self._listing_url_identity(expected) == self._listing_url_identity(response.url)

    def _khov_new_construction_path_parts(self, url: str) -> Optional[List[str]]:
        p = urlparse((url or "").strip())
        parts = [s for s in p.path.split("/") if s]
        if len(parts) < 4 or parts[0] != "new-construction-homes":
            return None
        return parts

    def _khov_has_subcommunity_listing_segment(self, url: str) -> bool:
        """True when URL is deeper than /new-construction-homes/{state}/{city}/{community}/ (plan, lot, QMI, etc.)."""
        parts = self._khov_new_construction_path_parts(url)
        if parts is None:
            return False
        return len(parts) >= 5

    def _listing_gallery_payload_present(self, blobs: List[Any]) -> bool:
        """Whether parsed JSON contains listing.gallery or the four HeroGallery* buckets."""
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            inner = node.get("gallery") or node.get("Gallery")
            if isinstance(inner, dict) and self._listing_gallery_has_required_shape(inner):
                return True
            if self._listing_gallery_has_required_shape(node):
                return True
        for node in self._iter_json(blobs):
            if isinstance(node, dict) and self._hero_gallery_container_has_shape(node):
                return True
        return False

    def _khov_should_skip_shell_listing_page(self, requested_url: str, blobs: List[Any]) -> bool:
        """Skip plan/lot URLs that only return the community shell (no listing image JSON), e.g. invalid Calder routes."""
        if not self._khov_has_subcommunity_listing_segment(requested_url):
            return False
        return not self._listing_gallery_payload_present(blobs)

    def _enforce_qmi_field_routing(self, data: Dict[str, Any], is_qmi: bool) -> None:
        """No-op: keep both model and QMI fields when values are available."""
        return

    def parse(self, response: scrapy.http.Response) -> Any:
        if response.status in self.REDIRECT_STATUS_CODES:
            self.logger.info("Skipped redirected URL: %s", response.request.url)
            return
        if response.status != 200:
            self.logger.info("Skipped non-200 URL: %s (status=%s)", response.request.url, response.status)
            return
        if not self._response_matches_expected_fetch(response):
            self.logger.info(
                "Skipped URL that redirected to a different page: %s -> %s",
                response.request.meta.get("expected_fetch_url", response.request.url),
                response.url,
            )
            return

        data: Dict[str, Any] = {k: "" for k in self.FIELDNAMES}
        data["url"] = response.url
        data["listing_url"] = response.url
        data["Website"] = "https://www.khov.com/"
        data["Builder"] = "K. Hovnanian Homes"
        next_payload_strings = self._extract_next_payload_strings(response)
        stream_text = self._normalized_stream_text("\n".join(next_payload_strings))
        qmi_block = self._extract_qmi_block(stream_text, response.url)

        path_state_city = self._state_city_from_url(response.url)
        if path_state_city:
            state_slug, city_slug = path_state_city
            data["State"] = state_slug.upper()
            data["City"] = city_slug.replace("-", " ").title()

        all_json = self._page_json_blobs(response, next_payload_strings, stream_text)
        expected_row_url = response.request.meta.get("expected_fetch_url") or response.url
        if self._khov_should_skip_shell_listing_page(expected_row_url, all_json):
            self.logger.info(
                "Skipped redirected URL: %s",
                expected_row_url,
            )
            return

        has_quick_move_container = self._has_quick_move_container(response)
        if has_quick_move_container:
            first_qmi_record = self._first_qmi_record_from_json(all_json)
            # Listing pages may contain many homes; use first home's QMI payload only.
            qmi_block = json.dumps(first_qmi_record, ensure_ascii=False) if first_qmi_record else ""
        is_qmi_page = self._is_qmi_page(response.url, qmi_block, has_quick_move_container)
        data["Adult Community (Y/N)"] = ""
        data["status"] = self._extract_status(stream_text, qmi_block, response)

        # Community / model info from embedded JSON
        community_name = (
            self._first_str_from_json(all_json, {"communityName", "community_name", "community"})
            or self._first_str_from_json(all_json, {"subdivision", "neighborhood"})
            or self._community_slug_from_url(response.url)
        )
        if community_name:
            data["Community Name"] = self._clean_text(community_name)

        qmi_model_name = (
            self._first_str_from_json(all_json, {"QMIName"})
            or self._extract_string(qmi_block, "QMIName")
            or self._extract_string(stream_text, "QMIName")
            or self._first_str_from_json(all_json, {"QMIModelName", "qmiModelName", "QMImodelname"})
            or self._extract_string(qmi_block, "QMIModelName")
            or self._extract_string(stream_text, "QMIModelName")
            or self._extract_string(qmi_block, "QMImodelname")
            or self._extract_string(stream_text, "QMImodelname")
            or self._first_str_from_json(all_json, {"PageTitle"})
        )
        model_types_found = self._has_model_product_types_in_page(response)
        data["Model/Product Types Available"] = "Y" if model_types_found else ""
        model_name = self._model_slug_from_url(response.url)
        data["Model Name"] = ""
        if qmi_model_name:
            data["QMI Model Name"] = self._clean_text(qmi_model_name)
        elif is_qmi_page and model_name:
            # Keep QMI model name populated even when payload omits QMIName keys.
            data["QMI Model Name"] = self._clean_text(model_name)

        # Address / geo from Next.js copy.first, JSON-LD (Place/PostalAddress), or embedded state
        copy_first_address = self._extract_copy_first_address(all_json)
        if copy_first_address:
            data["Street Address"] = copy_first_address
        addr, geo = self._address_and_geo_from_json(all_json)
        if addr:
            data["Street Address"] = data["Street Address"] or addr.get("street", "")
            data["City"] = data["City"] or addr.get("city", "")
            data["State"] = data["State"] or addr.get("state", "")
            data["Zip Code"] = addr.get("zip", "")
            data["County"] = addr.get("county", "")
        if geo:
            data["latitude"] = geo.get("lat", "")
            data["longitude"] = geo.get("lng", "")
        data["latitude"] = data["latitude"] or self._lookup_value(
            all_json, ["Latitude", "latitude", "lat", "SalesCenterLatitude"]
        )
        data["longitude"] = data["longitude"] or self._lookup_value(
            all_json, ["Longitude", "longitude", "lng", "lon", "SalesCenterLongitude"]
        )
        data["County"] = data["County"] or self._lookup_value(all_json, ["County", "county"])
        data["latitude"] = data["latitude"] or (
            response.xpath('//*[@data-lat]/@data-lat | //script[contains(text(),"latitude")]/text()').get("") or ""
        )
        data["longitude"] = data["longitude"] or (
            response.xpath('//*[@data-lng]/@data-lng | //script[contains(text(),"longitude")]/text()').get("") or ""
        )
        data["latitude"] = data["latitude"] or self._extract_first_numeric_string(
            stream_text, ["latitude", "Latitude", "lat", "Lat", "SalesCenterLatitude"]
        )
        data["longitude"] = data["longitude"] or self._extract_first_numeric_string(
            stream_text, ["longitude", "Longitude", "lng", "Lng", "lon", "Lon", "SalesCenterLongitude"]
        )
        data["latitude"] = self._extract_numeric_fragment(data["latitude"])
        data["longitude"] = self._extract_numeric_fragment(data["longitude"])

        # KHov stores many useful QMI fields in streamed Next.js data instead of JSON-LD.
        data["Street Address"] = data["Street Address"] or self._extract_string(qmi_block, "StreetAddress")
        data["City"] = data["City"] or self._extract_string(qmi_block, "City")
        data["State"] = data["State"] or self._extract_string(qmi_block, "State")
        data["Zip Code"] = data["Zip Code"] or self._extract_string(qmi_block, "ZipCode")
        data["Street Address"] = data["Street Address"] or self._extract_string(stream_text, "StreetAddress")
        data["City"] = data["City"] or self._extract_string(stream_text, "City")
        data["State"] = data["State"] or self._extract_string(stream_text, "State")
        data["Zip Code"] = data["Zip Code"] or self._extract_string(stream_text, "ZipCode")
        parsed_addr = self._extract_address_from_text(response)
        if parsed_addr:
            data["Street Address"] = data["Street Address"] or parsed_addr.get("street", "")
            data["City"] = data["City"] or parsed_addr.get("city", "")
            data["State"] = data["State"] or parsed_addr.get("state", "")
            data["Zip Code"] = data["Zip Code"] or parsed_addr.get("zip", "")

        # Phone / hours often appear in header/footer or contact blocks
        phone = self._extract_phone(response, stream_text)
        if phone:
            data["Phone number"] = phone

        hours = self._extract_hours(stream_text, response.text)
        if hours is not None:
            data["Model hours"] = hours

        # Descriptions / features from page response and structured JSON only.
        info_block_desc = self._extract_infoblock_description(response)
        overall_desc = self._extract_overall_description_from_response(response)
        qmi_info_desc = self._extract_qmi_info_description(all_json, qmi_block, stream_text)
        desc = self._first_str_from_json(all_json, {"description", "summary", "overview"})
        data["Overall Description of the Community"] = (
            data["Overall Description of the Community"]
            or (self._clean_rich_text(qmi_info_desc) if qmi_info_desc else "")
            or self._clean_rich_text(overall_desc)
            or (self._clean_rich_text(desc) if desc else "")
        )
        data["Plan Description"] = (
            data["Plan Description"]
            or (self._clean_rich_text(qmi_info_desc) if qmi_info_desc else "")
            or self._clean_rich_text(info_block_desc)
            or (self._clean_rich_text(desc) if desc else "")
        )

        # Bedrooms / bathrooms / garages from Next.js flight tuple[3].props only
        # (Override fields win over base fields).
        flight_dims = self._extract_home_dimensions_from_next_flight(all_json)
        beds = float(flight_dims["bedrooms"]) if flight_dims["bedrooms"] is not None else None
        baths = float(flight_dims["fullBaths"]) if flight_dims["fullBaths"] is not None else None
        half_baths = float(flight_dims["halfBaths"]) if flight_dims["halfBaths"] is not None else None
        garages = float(flight_dims["garages"]) if flight_dims["garages"] is not None else None

        # Fallback from DetailHeader summary values:
        # //ul[contains(@class,'DetailHeader')]/li/span/text()
        # map "Beds" -> BR, "Baths" -> BA, "Cars"/"Car" -> Garages.
        detail_dims = self._extract_dimensions_from_detail_header(response)
        if beds is None and detail_dims["bedrooms"] is not None:
            beds = detail_dims["bedrooms"]
        if garages is None and detail_dims["garages"] is not None:
            garages = detail_dims["garages"]
        if baths is None and detail_dims["baths"] is not None:
            baths_raw = detail_dims["baths"]
            whole_baths = int(baths_raw)
            fractional = baths_raw - whole_baths
            baths = float(whole_baths)
            half_baths = 1.0 if abs(fractional - 0.5) < 1e-9 else 0.0
        sqft = self._extract_number(qmi_block, "SquareFootage") or self._first_number_from_json(
            all_json, {"sqft", "squareFeet", "minSqft", "min_sqft"}
        )
        price = (
            self._extract_number(qmi_block, "CurrentPrice")
            or self._extract_number(stream_text, "CurrentPrice")
            or self._extract_number(qmi_block, "BasePrice")
            or self._extract_number(stream_text, "BasePrice")
            or self._extract_number(qmi_block, "MinPrice")
            or self._extract_number(stream_text, "MinPrice")
            or self._first_number_from_json(all_json, {"basePrice", "BasePrice", "minPrice", "MinPrice", "currentPrice", "CurrentPrice"})
        )
        current_price = self._extract_number(qmi_block, "CurrentPrice") or self._extract_number(stream_text, "CurrentPrice")
        lowest_priced_qmi = self._extract_number(qmi_block, "LowestPricedQMIPrice") or self._extract_number(
            stream_text, "LowestPricedQMIPrice"
        )
        # No UI/address-number fallbacks for bed/bath counts.
        if sqft is None:
            sqft = self._extract_ui_number(response.text, r"([\d,]+)\s*Sq\.?\s*Ft")
        if price is None:
            price = self._extract_ui_number(response.text, r"Current total price</h4><h6[^>]*>\$([\d,]+)")

        if beds is not None:
            v = self._number_to_str(beds)
            data["# BR"] = v
            data["QMI # of BR"] = v
        if baths is not None:
            v = self._number_to_str(baths)
            data["# BA"] = v
            data["QMI # of BA"] = v
        if half_baths is not None:
            v = self._number_to_str(half_baths)
            data["# 1/2 BA"] = v
            data["QMI # of 1/2 BA"] = v
        if sqft:
            v = self._number_to_str(sqft)
            data["Minimum SQFT"] = v
            data["QMI Model SQFT"] = v
        qmi_model_price = self._extract_current_total_price_from_response(response)
        if qmi_model_price is not None:
            v = self._number_to_str(qmi_model_price)
            data["QMI Model Price"] = v
            data["Minimum Base Price (Current)"] = v
        elif price:
            v = self._number_to_str(price)
            data["QMI Model Price"] = v
            data["Minimum Base Price (Current)"] = v
        price_range = self._extract_price_range_from_text(f"{stream_text} {response.text}")
        if price_range and not is_qmi_page:
            range_min, range_max = price_range
            data["Minimum Base Price (Current)"] = self._number_to_str(range_min)
            data["Maximum Base Price/All In Price"] = self._number_to_str(range_max)
        if (
            current_price is not None
            and lowest_priced_qmi is not None
            and abs(current_price - lowest_priced_qmi) < 1e-9
            and not is_qmi_page
        ):
            data["Minimum Base Price (Current)"] = self._number_to_str(current_price)
        previous_price = self._extract_number(qmi_block, "LastPrice")
        if previous_price is None:
            previous_price = self._extract_number(stream_text, "LastPrice")
        if previous_price:
            data["Previous Price"] = self._number_to_str(previous_price)

        if garages is not None:
            garage_value = self._number_to_str(garages)
            data["# Garages"] = garage_value
            data["# of Garages"] = garage_value
            data["QMI # of Garages"] = garage_value
            data["Garages (Y/N)"] = "Y" if garages and int(garages) > 0 else "N"

        floors = self._extract_number(qmi_block, "Stories")
        if floors is not None:
            data["# of Floors"] = self._number_to_str(floors)

        move_in_date = self._extract_string(qmi_block, "MoveInDate")
        if not move_in_date:
            move_in_date = self._extract_string(stream_text, "MoveInDate")
        normalized_move_in_date = self._normalize_move_in_date(move_in_date)
        if normalized_move_in_date:
            data["QMI Availability Date"] = normalized_move_in_date

        data["Avg Lot Size"] = data["Avg Lot Size"] or self._lookup_value(all_json, ["LotSqft", "LotSize", "lotSqft", "lotSize"])
        data["Avg Lot - Width/Depth"] = data["Avg Lot - Width/Depth"] or self._lookup_value(
            all_json, ["LotWidthDepth", "LotDimensions", "LotWidth", "LotDepth"]
        )
        data["Foundation Type"] = data["Foundation Type"] or self._lookup_value(
            all_json, ["FoundationType", "foundationType"]
        )
        data["Exterior Specifications Available"] = data["Exterior Specifications Available"] or self._lookup_value(
            all_json, ["ExteriorSpecificationsAvailable", "ExteriorSpecifications", "ExteriorSpecs"]
        )
        data["Interior Specifications Available"] = data["Interior Specifications Available"] or self._lookup_value(
            all_json, ["InteriorSpecificationsAvailable", "InteriorSpecifications", "InteriorSpecs"]
        )
        hoa_existing = str(data.get("HOA Fee") or "").strip()
        if hoa_existing:
            data["HOA Fee"] = hoa_existing
        else:
            hoa_num = self._first_json_number_as_str(
                (qmi_block, "HOAFees"),
                (stream_text, "HOAFees"),
                (qmi_block, "HOAFee"),
                (stream_text, "HOAFee"),
            )
            if hoa_num != "":
                data["HOA Fee"] = hoa_num
            else:
                data["HOA Fee"] = self._extract_string(qmi_block, "HOAFees") or self._extract_string(
                    stream_text, "HOAFees"
                )
        data["HOA Services"] = data["HOA Services"] or self._lookup_value(
            all_json, ["HOAServices", "hoaServices", "HoaServices"]
        )
        data["Other Fees (i.e. CDD)"] = data["Other Fees (i.e. CDD)"] or self._lookup_value(
            all_json, ["CDD", "CDDFee", "cddFee", "OtherFees", "otherFees"]
        )
        data["City/Town/Property Tax %"] = (
            data["City/Town/Property Tax %"]
            or self._extract_number(qmi_block, "PropertyTaxRate")
            or self._extract_number(stream_text, "PropertyTaxRate")
            or self._extract_number(qmi_block, "propertyTaxRate")
            or self._extract_number(stream_text, "propertyTaxRate")
            or self._extract_string(qmi_block, "PropertyTaxRate")
            or self._extract_string(stream_text, "PropertyTaxRate")
            or self._lookup_value(all_json, ["PropertyTaxRate", "propertyTaxRate", "TaxRate", "taxRate", "PropertyTax", "CityTax"])
        )
        data["Sales Start Date"] = data["Sales Start Date"] or self._lookup_value(
            all_json, ["SalesStartDate", "salesStartDate"]
        )
        data["Sold Out Date"] = data["Sold Out Date"] or self._lookup_value(all_json, ["SoldOutDate", "soldOutDate"])
        data["Total Lots Sold"] = data["Total Lots Sold"] or self._lookup_value(
            all_json, ["TotalLotsSold", "LotsSold", "totalLotsSold"]
        )
        data["Incentives"] = data["Incentives"] or self._lookup_value(
            all_json, ["Incentives", "incentives", "Incentive", "incentive"]
        )
        if is_qmi_page:
            data["QMI Incentives"] = (
                data["QMI Incentives"]
                or self._extract_string(qmi_block, "QMIIncentives")
                or self._extract_string(qmi_block, "QMIIncentive")
                or self._extract_string(stream_text, "QMIIncentives")
                or self._extract_string(stream_text, "QMIIncentive")
                or self._lookup_value(all_json, ["QMIIncentives", "qmiIncentives", "QMIIncentive", "qmiIncentive"])
            )
            if not data["QMI # of 1/2 BA"]:
                qmi_half = self._extract_number(qmi_block, "QMIHalfBaths")
                if qmi_half is None:
                    qmi_half = self._extract_number(qmi_block, "HalfBaths")
                if qmi_half is None:
                    qmi_half_txt = self._extract_string(stream_text, "QMIHalfBaths") or self._extract_string(
                        stream_text, "HalfBaths"
                    )
                    if qmi_half_txt:
                        data["QMI # of 1/2 BA"] = qmi_half_txt
                else:
                    data["QMI # of 1/2 BA"] = self._number_to_str(qmi_half)
            data["QMI Lot SQFT"] = (
                data["QMI Lot SQFT"]
                or self._extract_number(qmi_block, "QMILotSqft")
                or self._extract_number(qmi_block, "LotSqft")
                or self._extract_string(stream_text, "QMILotSqft")
                or self._extract_string(stream_text, "LotSqft")
                or self._lookup_value(all_json, ["QMILotSqft", "LotSqft", "lotSqft"])
            )
        else:
            data["# 1/2 BA"] = data["# 1/2 BA"] or self._lookup_value(all_json, ["HalfBaths", "halfBaths"])
        data["Model Details"] = data["Model Details"] or self._lookup_value(
            all_json, ["ModelDetails", "modelDetails", "FloorplanDescription"]
        )
        data["Plan Garage Entry (Front load)"] = data["Plan Garage Entry (Front load)"] or self._lookup_value(
            all_json, ["PlanGarageEntry", "GarageEntry", "garageEntry"]
        )
        data["Last Updated"] = data["Last Updated"] or self._lookup_value(all_json, ["LastUpdated", "lastUpdated"])
        data["Plan Features"] = data["Plan Features"] or self._lookup_value(all_json, ["PlanFeatures", "planFeatures"])
        data["Interior Specifications Descriptions"] = data["Interior Specifications Descriptions"] or self._lookup_value(
            all_json, ["InteriorSpecificationDescriptions", "InteriorSpecificationsDescriptions"]
        )
        data["Attributes/Features"] = (
            data["Attributes/Features"]
            or self._lookup_value(
                all_json,
                [
                    "Attributes",
                    "Attribute",
                    "Features",
                    "Feature",
                    "CommunityFeatures",
                    "communityFeatures",
                    "AttributesFeatures",
                ],
            )
        )
        amenity_types = self._unique_preserve_order(
            self._extract_all_strings(stream_text, "AmenityName")
            + self._collect_strings_from_json(all_json, {"AmenityName", "amenityName"})
        )
        if amenity_types:
            data["Amenity Type"] = ",".join(amenity_types)

        product_type = self._infer_product_type(stream_text, qmi_block)
        if product_type:
            data["Product Type (SFD/SFA/CO)"] = product_type
        if data["# Garages"] and not data["Parking Type"]:
            data["Parking Type"] = "Garage"
        # Pricing rule: when CurrentPrice exists, store it as maximum/all-in price.
        if not is_qmi_page:
            current_max_price = qmi_model_price if qmi_model_price is not None else current_price
            if current_max_price is not None:
                data["Maximum Base Price/All In Price"] = self._number_to_str(current_max_price)
            else:
                data["Maximum Base Price/All In Price"] = data["Maximum Base Price/All In Price"] or ""

        images = self._extract_images(response, all_json)
        data["images"] = json.dumps(images, ensure_ascii=False)

        # Community landing URL is only for follow-up requests; text goes in Community Details.
        community_url = self._community_details_url(response.url)
        data["Community Details"] = self._extract_community_details_from_response(
            response, data.get("Community Name", "")
        )
        if community_url and community_url.rstrip("/") != response.url.rstrip("/"):
            self._enforce_qmi_field_routing(data, is_qmi_page)
            yield scrapy.Request(
                url=community_url,
                callback=self.parse_community,
                errback=self.errback,
                dont_filter=True,
                meta={
                    "item": data,
                    "is_qmi_page": is_qmi_page,
                    "expected_fetch_url": community_url,
                    "dont_redirect": True,
                    "handle_httpstatus_list": sorted(self.REDIRECT_STATUS_CODES),
                },
            )
            return

        self._enforce_qmi_field_routing(data, is_qmi_page)
        yield self._finish_item(data)

    def parse_community(self, response: scrapy.http.Response) -> Dict[str, Any]:
        if response.status in self.REDIRECT_STATUS_CODES:
            self.logger.info("Skipped redirected URL: %s", response.request.url)
            return self._finish_item(dict(response.meta["item"]))
        if response.status != 200:
            self.logger.info("Skipped non-200 URL: %s (status=%s)", response.request.url, response.status)
            return self._finish_item(dict(response.meta["item"]))
        if not self._response_matches_expected_fetch(response):
            self.logger.info(
                "Skipped community URL that redirected to a different page: %s -> %s",
                response.request.meta.get("expected_fetch_url", response.request.url),
                response.url,
            )
            return self._finish_item(dict(response.meta["item"]))

        data = dict(response.meta["item"])
        community_fields = self._extract_community_fields(response)
        self._merge_blank_fields(data, community_fields)
        self._enforce_qmi_field_routing(data, bool(response.meta.get("is_qmi_page")))
        data["Community Details"] = self._extract_community_details_from_response(
            response, data.get("Community Name", "")
        )
        return self._finish_item(data)

    def _extract_embedded_json(self, response: scrapy.http.Response) -> List[Any]:
        out: List[Any] = []
        # Next.js
        next_data = response.css("script#__NEXT_DATA__::text").get()
        if next_data:
            parsed = self._safe_json_loads(next_data)
            if parsed is not None:
                out.append(parsed)

        # Generic window.__APOLLO_STATE__ or similar
        for txt in response.css('script::text').getall():
            if "__APOLLO_STATE__" in txt or "apolloState" in txt or "pageProps" in txt:
                # try to pull JSON object substring
                maybe = self._extract_first_json_object(txt)
                if maybe:
                    parsed = self._safe_json_loads(maybe)
                    if parsed is not None:
                        out.append(parsed)
        return out

    def _page_json_blobs(
        self, response: scrapy.http.Response, next_payload_strings: List[str], stream_text: str
    ) -> List[Any]:
        scripts_json = self._extract_embedded_json(response)
        ld_json = self._extract_jsonld(response)
        merged = scripts_json + ld_json
        next_stream_objects = self._extract_next_stream_objects(next_payload_strings, stream_text)
        return next_stream_objects + merged

    def _extract_jsonld(self, response: scrapy.http.Response) -> List[Any]:
        out: List[Any] = []
        for txt in response.css('script[type="application/ld+json"]::text').getall():
            parsed = self._safe_json_loads(txt)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                out.extend(parsed)
            else:
                out.append(parsed)
        return out

    def _address_and_geo_from_json(self, blobs: List[Any]) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, str]]]:
        addr: Dict[str, str] = {}
        geo: Dict[str, str] = {}

        # Try JSON-LD Place -> address/geo
        for node in self._iter_json(blobs): 
            if not isinstance(node, dict):
                continue

            # GeoCoordinates
            if ("latitude" in node and "longitude" in node) or node.get("@type") == "GeoCoordinates":
                lat = node.get("latitude")
                lng = node.get("longitude")
                if lat is not None and lng is not None and not geo:
                    geo = {"lat": str(lat), "lng": str(lng)}

            # PostalAddress
            if node.get("@type") == "PostalAddress" or ("streetAddress" in node and ("addressLocality" in node or "addressRegion" in node)):
                street = node.get("streetAddress") or node.get("street") or ""
                city = node.get("addressLocality") or node.get("city") or ""
                state = node.get("addressRegion") or node.get("state") or ""
                zip_code = node.get("postalCode") or node.get("zip") or ""
                if street and not addr.get("street"):
                    addr = {
                        "street": self._clean_text(str(street)),
                        "city": self._clean_text(str(city)),
                        "state": self._clean_text(str(state)),
                        "zip": self._clean_text(str(zip_code)),
                        "county": self._clean_text(str(node.get("addressCounty") or node.get("county") or "")),
                    }

            # Common non-ld-json keys
            if not addr.get("street"):
                street = node.get("streetAddress") or node.get("address1") or node.get("address") or ""
                city = node.get("city") or ""
                state = node.get("state") or node.get("stateCode") or ""
                zip_code = node.get("zip") or node.get("postalCode") or ""
                if street and (city or state or zip_code):
                    addr = {
                        "street": self._clean_text(str(street)),
                        "city": self._clean_text(str(city)),
                        "state": self._clean_text(str(state)),
                        "zip": self._clean_text(str(zip_code)),
                        "county": self._clean_text(str(node.get("county") or node.get("addressCounty") or "")),
                    }

            if not geo and ("lat" in node and "lng" in node):
                lat = node.get("lat")
                lng = node.get("lng")
                if lat is not None and lng is not None:
                    geo = {"lat": str(lat), "lng": str(lng)}

        return (addr or None, geo or None)

    def _extract_phone(self, response: scrapy.http.Response, stream_text: str = "") -> str:
        sales_phone = self._extract_string(stream_text, "SalesCenterPhoneNumber")
        if sales_phone:
            return sales_phone

        info_phone = self._extract_string(stream_text, "InformationSpecialistPhoneNumber")
        if info_phone:
            return info_phone

        text = " ".join(response.css("body *::text").getall())
        text = re.sub(r"\s+", " ", text)
        # North America phone formats
        m = re.search(r"(\+?1[\s\-\.]?)?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}", text)
        return m.group(0).strip() if m else ""

    def _extract_hours(self, stream_text: str, response_text: str = "") -> Optional[str]:
        source = stream_text
        if response_text:
            source = f"{source} {self._normalized_stream_text(response_text)}"
        hours_section = self._extract_balanced_section(source, '"HoursOfOperations":[')
        if not hours_section:
            return None

        parsed = self._safe_json_loads(hours_section)
        if not isinstance(parsed, list) or not parsed:
            return None

        rows: List[str] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue

            expanded = item.get("ContentLink")
            if isinstance(expanded, dict):
                expanded = expanded.get("Expanded")
            if not isinstance(expanded, dict):
                continue

            day = self._clean_text(str(expanded.get("DayOfWeek") or ""))
            if not day:
                continue

            by_appt = expanded.get("ByAppointmentOnly")
            opens = self._clean_text(str(expanded.get("Opens") or ""))
            closes = self._clean_text(str(expanded.get("Closes") or ""))

            if isinstance(by_appt, bool) and by_appt:
                rows.append(f"{day}: By Appointment Only")
                continue
            if not opens or not closes:
                # Skip entries with missing values.
                continue
            rows.append(f"{day}: {opens}-{closes}")

        if not rows:
            return None

        model_hours = ", ".join(rows)
        return model_hours

    def _meta_description(self, response: scrapy.http.Response) -> str:
        return (response.css('meta[name="description"]::attr(content)').get() or "").strip()

    def _extract_community_details_text(
        self,
        response: scrapy.http.Response,
        blobs: List[Any],
        *,
        allow_meta_fallback: bool = False,
    ) -> str:
        keys = {
            "communityDescription",
            "community_description",
            "CommunityDescription",
            "subdivisionDescription",
            "neighborhoodDescription",
            "NeighborhoodDescription",
            "communityOverview",
            "CommunityOverview",
            "salesCenterDescription",
            "SalesCenterDescription",
        }
        found = self._clean_text(self._first_str_from_json(blobs, keys))
        if self._is_valid_community_details(found):
            return found
        if allow_meta_fallback:
            meta = self._clean_text(self._meta_description(response))
            if self._is_valid_community_details(meta):
                return meta
        return ""

    def _is_valid_community_details(self, text: str) -> bool:
        txt = self._clean_text(text)
        if not txt:
            return False
        if txt.lower().startswith(("http://", "https://", "www.")):
            return False
        # Keep only meaningful descriptions; avoid short labels/placeholders.
        if len(txt) < 40:
            return False
        if len(re.findall(r"[A-Za-z]{2,}", txt)) < 6:
            return False
        return True

    def _matches_community_name(self, details_text: str, community_name: str) -> bool:
        details = self._clean_text(details_text).lower()
        name = self._clean_text(community_name).lower()
        if not details:
            return False
        if not name:
            return True
        if name in details:
            return True
        name_tokens = [t for t in re.findall(r"[a-z0-9]+", name) if len(t) >= 4]
        if not name_tokens:
            return True
        return any(tok in details for tok in name_tokens)

    def _extract_community_details_from_response(self, response: scrapy.http.Response, community_name: str) -> str:
        blocks = [
            self._extract_infoblock_description(response),
            self._clean_text(" ".join(response.css("section[class*='overview'] *::text").getall())),
            self._clean_text(" ".join(response.css("section[class*='description'] *::text").getall())),
            self._clean_text(" ".join(response.css("div[class*='overview'] *::text").getall())),
            self._clean_text(" ".join(response.css("div[class*='description'] *::text").getall())),
        ]
        for txt in blocks:
            if not self._is_valid_community_details(txt):
                continue
            if self._matches_community_name(txt, community_name):
                return txt
        return ""

    def _extract_next_payload_strings(self, response: scrapy.http.Response) -> List[str]:
        chunks: List[str] = []
        for script_text in response.css("script::text").getall():
            if "self.__next_f.push(" not in script_text:
                continue
            for payload in self._extract_next_push_payloads(script_text):
                chunks.extend(self._decode_next_push_payload(payload))
        return chunks

    def _extract_next_push_payloads(self, script_text: str) -> List[str]:
        payloads: List[str] = []
        marker = "self.__next_f.push("
        start = 0
        while True:
            idx = script_text.find(marker, start)
            if idx == -1:
                break
            open_idx = idx + len(marker)
            depth = 1
            i = open_idx
            while i < len(script_text) and depth > 0:
                c = script_text[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                i += 1
            payload = script_text[open_idx : i - 1].strip()
            if payload:
                payloads.append(payload)
            start = i
        return payloads

    def _decode_next_push_payload(self, payload: str) -> List[str]:
        payload = (payload or "").strip()
        if not payload:
            return []
        decoded_texts: List[str] = []
        parsed_payload = self._safe_json_loads(payload)
        if isinstance(parsed_payload, list) and len(parsed_payload) >= 2 and isinstance(parsed_payload[1], str):
            source = parsed_payload[1]
            for line in source.split("\n"):
                chunk = line.strip()
                if not chunk:
                    continue
                m = re.match(r"^[0-9a-zA-Z]+:(.*)$", chunk, re.DOTALL)
                decoded_texts.append((m.group(1) if m else chunk).strip())
        else:
            decoded_texts.append(payload)
        return [txt for txt in decoded_texts if txt]

    def _extract_next_stream_objects(self, next_chunks: List[str], stream_text: str) -> List[Any]:
        objects: List[Any] = []
        seen = set()

        def collect_from_text(text: str) -> None:
            decoder = json.JSONDecoder()
            i = 0
            while i < len(text):
                while i < len(text) and text[i] not in "[{":
                    i += 1
                if i >= len(text):
                    break
                try:
                    parsed, end = decoder.raw_decode(text, i)
                except Exception:
                    i += 1
                    continue
                key = repr(parsed)
                if key not in seen:
                    seen.add(key)
                    objects.append(parsed)
                i = max(i + 1, end)

        for chunk in next_chunks:
            collect_from_text(chunk)
        collect_from_text(stream_text)
        return objects

    def _normalized_stream_text(self, text: str) -> str:
        cleaned = (text or "")
        cleaned = cleaned.replace("\\n", " ")
        cleaned = cleaned.replace('\\"', '"')
        cleaned = cleaned.replace("\\u003c", "<")
        cleaned = cleaned.replace("\\u003e", ">")
        cleaned = cleaned.replace("\\u0026", "&")
        cleaned = cleaned.replace("\\/", "/")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    def _extract_qmi_block(self, stream_text: str, url: str) -> str:
        url = url.rstrip("/")
        relative_path = "/" + "/".join(url.split("/")[3:])
        match_pos = -1
        for needle in (f'"Url":"{url}/"', f'"Url":"{url}"', f'"RelativePath":"{relative_path}/"', f'"RelativePath":"{relative_path}"'):
            match_pos = stream_text.find(needle)
            if match_pos != -1:
                break
        if match_pos == -1:
            return ""

        start = stream_text.rfind("{", 0, match_pos)
        if start == -1:
            return stream_text[max(0, match_pos - 6000) : match_pos + 6000]

        depth = 0
        for i in range(start, len(stream_text)):
            c = stream_text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return stream_text[start : i + 1]
        return stream_text[max(0, match_pos - 6000) : match_pos + 6000]

    def _extract_balanced_section(self, text: str, marker: str) -> str:
        pos = text.find(marker)
        if pos == -1:
            return ""

        start = text.find("[", pos)
        if start == -1:
            return ""

        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return ""

    def _extract_string(self, text: str, key: str) -> str:
        if not text:
            return ""
        m = re.search(rf'"{re.escape(key)}":"(.*?)"', text)
        return self._clean_text(m.group(1)) if m else ""

    def _extract_number(self, text: str, key: str) -> Optional[float]:
        if not text:
            return None
        m = re.search(rf'"{re.escape(key)}":\s*(-?\d+(?:\.\d+)?)', text)
        if not m:
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    def _number_from_value(self, value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            txt = value.strip().replace(",", "")
            if re.fullmatch(r"-?\d+(?:\.\d+)?", txt):
                try:
                    return float(txt)
                except Exception:
                    return None
        return None

    def _extract_home_dimensions_from_next_flight(self, blobs: List[Any]) -> Dict[str, Optional[int]]:
        """
        Extract beds/baths/garages from Next.js RSC tuple[3].props.
        Override fields are preferred:
          BedsOverride ?? Beds
          BathsOverride ?? Baths
          GaragesOverride ?? Garages
        Baths .5 means exactly one half-bath.
        """
        def dims_from_props(props: Dict[str, Any]) -> Dict[str, Optional[int]]:
            beds_n = self._number_from_value(
                props.get("BedsOverride") if props.get("BedsOverride") is not None else props.get("Beds")
            )
            baths_n = self._number_from_value(
                props.get("BathsOverride") if props.get("BathsOverride") is not None else props.get("Baths")
            )
            garages_n = self._number_from_value(
                props.get("GaragesOverride") if props.get("GaragesOverride") is not None else props.get("Garages")
            )

            bedrooms = int(beds_n) if beds_n is not None else None
            garages = int(garages_n) if garages_n is not None else None
            full_baths: Optional[int] = None
            half_baths: Optional[int] = None
            if baths_n is not None:
                full_baths = int(baths_n)
                fractional = baths_n - full_baths
                half_baths = 1 if abs(fractional - 0.5) < 1e-9 else 0

            return {
                "bedrooms": bedrooms,
                "fullBaths": full_baths,
                "halfBaths": half_baths,
                "garages": garages,
            }

        # Primary: tuple[3].props shape from Next.js flight chunks.
        for node in self._iter_json(blobs):
            if not isinstance(node, list) or len(node) <= 3:
                continue
            props_holder = node[3]
            if not isinstance(props_holder, dict):
                continue
            props = props_holder.get("props")
            if not isinstance(props, dict):
                continue
            out = dims_from_props(props)
            if any(v is not None for v in out.values()):
                return out

        # Fallback: some pages embed the same props object at other nesting levels.
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            props = node.get("props") if isinstance(node.get("props"), dict) else node
            if not isinstance(props, dict):
                continue
            if not any(k in props for k in ("BedsOverride", "Beds", "BathsOverride", "Baths", "GaragesOverride", "Garages")):
                continue
            out = dims_from_props(props)
            if any(v is not None for v in out.values()):
                return out

        return {"bedrooms": None, "fullBaths": None, "halfBaths": None, "garages": None}

    def _extract_dimensions_from_detail_header(self, response: scrapy.http.Response) -> Dict[str, Optional[float]]:
        """
        Fallback parser for:
        //ul[contains(@class,'DetailHeader')]/li/span/text()
        Uses token keywords:
          - contains 'bed'  -> bedrooms
          - contains 'bath' -> baths
          - contains 'car'  -> garages
        """
        out: Dict[str, Optional[float]] = {"bedrooms": None, "baths": None, "garages": None}
        texts = response.xpath("//ul[contains(@class,'DetailHeader')]/li/span/text()").getall()
        for raw in texts:
            txt = self._clean_text(raw).lower()
            if not txt:
                continue
            m = re.search(r"(-?\d+(?:\.\d+)?)", txt)
            if not m:
                continue
            try:
                val = float(m.group(1))
            except Exception:
                continue
            if "Beds" in txt and out["bedrooms"] is None:
                out["bedrooms"] = val
            elif "Baths" in txt and out["baths"] is None:
                out["baths"] = val
            elif "Cars" in txt and out["garages"] is None:
                out["garages"] = val
            if all(v is not None for v in out.values()):
                break
        return out

    def _first_json_number_as_str(self, *text_key_pairs: Tuple[str, str]) -> str:
        """First JSON numeric match across (text, key) pairs; 0 and negatives are kept (not treated as missing)."""
        for text, key in text_key_pairs:
            value = self._extract_number(text, key)
            if value is not None:
                return self._number_to_str(value)
        return ""

    def _extract_ui_number(self, text: str, pattern: str) -> Optional[float]:
        if not text:
            return None
        m = re.search(pattern, text, re.I)
        if not m:
            return None
        value = next((g for g in m.groups() if g), None) if m.groups() else m.group(1)
        if not value:
            return None
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            return None

    def _extract_current_total_price_from_response(self, response: scrapy.http.Response) -> Optional[float]:
        price_text = response.xpath(
            '//h4[contains(normalize-space(.),"Current total price")]/following-sibling::h6[1]/text()'
        ).get("")
        if not price_text:
            price_text = response.css("h4.b1 + h6.h3::text").get("")
        if not price_text:
            return None
        m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", price_text)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    def _extract_price_range_from_text(self, text: str) -> Optional[Tuple[float, float]]:
        if not text:
            return None
        # Price range must be dollar-denominated and near price keywords.
        pattern = r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:-|to|–|—)\s*\$\s*([\d,]+(?:\.\d+)?)"
        for m in re.finditer(pattern, text, re.I):
            ctx_start = max(0, m.start() - 60)
            ctx = text[ctx_start : m.start()].lower()
            if not re.search(r"(price|priced|from|starting|base)", ctx):
                continue
            try:
                low = float(m.group(1).replace(",", ""))
                high = float(m.group(2).replace(",", ""))
            except ValueError:
                continue
            if low <= 0 or high <= 0:
                continue
            if high < low:
                low, high = high, low
            return (low, high)
        return None

    def _extract_first_numeric_string(self, text: str, keys: List[str]) -> str:
        if not text:
            return ""
        for key in keys:
            m = re.search(rf'"{re.escape(key)}":\s*(-?\d+(?:\.\d+)?)', text)
            if m:
                return m.group(1)
        return ""

    def _extract_numeric_fragment(self, text: str) -> str:
        if not text:
            return ""
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        return m.group(0) if m else ""

    def _normalize_move_in_date(self, value: str) -> str:
        """Return YYYY-MM-DD from MoveInDate, skipping placeholder sentinel dates."""
        txt = self._clean_text(value or "")
        if not txt:
            return ""
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", txt)
        if not m:
            return ""
        day = m.group(1)
        if day in {"2000-01-01"}:
            return ""
        return day

    def _extract_all_numbers(self, text: str, key: str) -> List[float]:
        if not text:
            return []
        values: List[float] = []
        for match in re.findall(rf'"{re.escape(key)}":(-?\d+(?:\.\d+)?)', text):
            try:
                values.append(float(match))
            except ValueError:
                continue
        return values

    def _extract_all_strings(self, text: str, key: str) -> List[str]:
        if not text:
            return []
        return [self._clean_text(v) for v in re.findall(rf'"{re.escape(key)}":"(.*?)"', text) if self._clean_text(v)]

    def _extract_copy_first_address(self, blobs: List[Any]) -> str:
        """Extract Street Address from Next.js payload path copy.first only."""
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            copy_node = node.get("copy")
            if not isinstance(copy_node, dict):
                continue
            first_value = copy_node.get("first")
            if isinstance(first_value, str):
                cleaned = self._clean_text(first_value)
                if cleaned:
                    return cleaned
        return ""

    def _extract_qmi_info_description(self, blobs: List[Any], *texts: str) -> str:
        """Read QMIInfoDescription safely (supports escaped quotes inside HTML attributes)."""
        from_json = self._first_str_from_json(blobs, {"QMIInfoDescription", "qmiInfoDescription"})
        if from_json:
            return from_json
        # Fallback for raw streamed text; handle escaped quotes like data-start=\"123\".
        pattern = r'"QMIInfoDescription"\s*:\s*"((?:\\.|[^"\\])*)"'
        for text in texts:
            if not text:
                continue
            m = re.search(pattern, text, flags=re.S)
            if m:
                return m.group(1)
        return ""

    def _parse_qmi_info_description_items(self, html_text: str) -> Optional[List[str]]:
        """Extract clean <li> text entries from QMIInfoDescription HTML only."""
        src = (html_text or "").strip()
        if not src:
            return None
        # QMIInfoDescription often arrives JSON-escaped (e.g. \u003c, \\n, \u0026amp;).
        # Decode escape sequences before HTML list parsing.
        decoded = src
        for _ in range(2):
            try:
                decoded = json.loads(f'"{decoded}"')
            except Exception:
                break
        # Fallback decode for partially escaped payloads.
        decoded = (
            str(decoded)
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0026", "&")
            .replace("\\n", "\n")
            .replace("\\/", "/")
        )
        src = self._clean_text(html.unescape(decoded))
        li_chunks = re.findall(r"<li[^>]*>(.*?)</li>", src, flags=re.I | re.S)
        if not li_chunks:
            return None
        out: List[str] = []
        for raw in li_chunks:
            txt = html.unescape(raw)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = self._clean_text(txt)
            if txt:
                out.append(txt)
        return out or None

    def _join_qmi_info_description_items(self, items: List[str]) -> str:
        cleaned = [self._clean_text(x) for x in (items or []) if self._clean_text(x)]
        if not cleaned:
            return ""
        sentence_parts = [cleaned[0]]
        for part in cleaned[1:]:
            sentence_parts.append(part[:1].lower() + part[1:] if part else part)
        return ", ".join(sentence_parts)

    def _parse_qmi_info_description_items(self, html_text: str) -> Optional[List[str]]:
        """Extract clean <li> text entries from QMIInfoDescription HTML only."""
        src = (html_text or "").strip()
        if not src:
            return None
        # QMIInfoDescription often arrives JSON-escaped (e.g. \u003c, \\n, \u0026amp;).
        # Decode escape sequences before HTML list parsing.
        decoded = src
        for _ in range(2):
            try:
                decoded = json.loads(f'"{decoded}"')
            except Exception:
                break
        # Fallback decode for partially escaped payloads.
        decoded = (
            str(decoded)
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0026", "&")
            .replace("\\n", "\n")
            .replace("\\/", "/")
        )
        src = self._clean_text(html.unescape(decoded))
        li_chunks = re.findall(r"<li[^>]*>(.*?)</li>", src, flags=re.I | re.S)
        if not li_chunks:
            return None
        out: List[str] = []
        for raw in li_chunks:
            txt = html.unescape(raw)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = self._clean_text(txt)
            if txt:
                out.append(txt)
        return out or None

    def _join_qmi_info_description_items(self, items: List[str]) -> str:
        cleaned = [self._clean_text(x) for x in (items or []) if self._clean_text(x)]
        if not cleaned:
            return ""
        sentence_parts = [cleaned[0]]
        for part in cleaned[1:]:
            sentence_parts.append(part[:1].lower() + part[1:] if part else part)
        return ", ".join(sentence_parts)

    def _parse_qmi_info_description_items(self, html_text: str) -> Optional[List[str]]:
        """Extract clean <li> text entries from QMIInfoDescription HTML only."""
        src = (html_text or "").strip()
        if not src:
            return None
        # QMIInfoDescription often arrives JSON-escaped (e.g. \u003c, \\n, \u0026amp;).
        # Decode escape sequences before HTML list parsing.
        decoded = src
        for _ in range(2):
            try:
                decoded = json.loads(f'"{decoded}"')
            except Exception:
                break
        # Fallback decode for partially escaped payloads.
        decoded = (
            str(decoded)
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0026", "&")
            .replace("\\n", "\n")
            .replace("\\/", "/")
        )
        src = self._clean_text(html.unescape(decoded))
        li_chunks = re.findall(r"<li[^>]*>(.*?)</li>", src, flags=re.I | re.S)
        if not li_chunks:
            return None
        out: List[str] = []
        for raw in li_chunks:
            txt = html.unescape(raw)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = self._clean_text(txt)
            if txt:
                out.append(txt)
        return out or None

    def _join_qmi_info_description_items(self, items: List[str]) -> str:
        cleaned = [self._clean_text(x) for x in (items or []) if self._clean_text(x)]
        if not cleaned:
            return ""
        sentence_parts = [cleaned[0]]
        for part in cleaned[1:]:
            sentence_parts.append(part[:1].lower() + part[1:] if part else part)
        return ", ".join(sentence_parts)

    def _lookup_value(self, blobs: List[Any], keys: List[str]) -> str:
        wanted = {k.lower() for k in keys}
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                if str(key).lower() not in wanted:
                    continue
                if value is None:
                    continue
                if isinstance(value, (int, float)):
                    return self._number_to_str(float(value))
                if isinstance(value, str) and value.strip():
                    return self._clean_text(value)
        return ""

    def _unique_preserve_order(self, values: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for value in values:
            if value not in seen:
                seen.add(value)
                out.append(value)
        return out

    def _infer_product_type(self, stream_text: str, qmi_block: str) -> str:
        source = f"{qmi_block} {stream_text}".lower()
        for token in ("single family detached", "single family", "townhome", "duplex", "condominium", "condo"):
            if token in source:
                return token
        return ""

    def _is_adult_community_signal(self, url: str, *sources: str) -> bool:
        combined = " ".join((s or "") for s in sources).lower()
        url_lower = (url or "").lower()
        # Restrict to explicit adult-community indicators from URL/script only.
        patterns = ("55+", "55 plus", "active adult", "age-restricted", "age restricted")
        explicit_keys = ("isadultcommunity\":true", "\"adultcommunity\":true", "\"activeadult\":true")
        if any(k in combined for k in explicit_keys):
            return True
        return any(p in combined for p in patterns) or any(p in url_lower for p in patterns)

    def _extract_status(self, stream_text: str, qmi_block: str, response: scrapy.http.Response) -> str:
        value = self._extract_string(qmi_block, "Status") or self._extract_string(stream_text, "Status")
        if value:
            normalized = self._normalize_status(value)
            if normalized:
                return normalized
        return ""

    def _normalize_status(self, value: str) -> str:
        value = self._clean_text(value)
        if not value:
            return ""

        lowered = value.lower()
        if (
            lowered.startswith("available")
            or lowered in {"now selling", "active", "open", "coming soon"}
        ):
            return "Active"
        if lowered in {"sold out", "closed", "inactive"}:
            return ""
        return ""

    def _listing_gallery_has_required_shape(self, d: Any) -> bool:
        if not isinstance(d, dict):
            return False
        for key in self._LISTING_GALLERY_KEYS:
            if key not in d:
                return False
            v = d[key]
            if v is not None and not isinstance(v, list):
                return False
        return True

    def _hero_gallery_container_has_shape(self, d: Any) -> bool:
        if not isinstance(d, dict):
            return False
        for _, hero_key in self._HERO_GALLERY_KEYS:
            if hero_key not in d:
                return False
            v = d[hero_key]
            if v is not None and not isinstance(v, list):
                return False
        return True

    def _url_from_gallery_or_hero_row(self, item: Any) -> str:
        """Single image row: canonical {url}; hero rows use ContentLink.Expanded (ImageBlock)."""
        if not isinstance(item, dict):
            return ""
        u = item.get("url")
        if isinstance(u, str) and u.strip():
            return self._normalize_image_url(u.strip())
        cl = item.get("ContentLink") or item.get("contentLink")
        if isinstance(cl, dict):
            expanded = cl.get("Expanded") or cl.get("expanded")
            if isinstance(expanded, dict):
                u2 = expanded.get("Url") or expanded.get("url")
                if isinstance(u2, str) and u2.strip():
                    return self._normalize_image_url(u2.strip())
        return ""

    def _canonical_gallery_from_hero_container(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """Build listing.gallery-shaped dict from the four HeroGallery* arrays only."""
        out: Dict[str, Any] = {}
        for canon, hero_key in self._HERO_GALLERY_KEYS:
            rows = node.get(hero_key)
            if not isinstance(rows, list):
                rows = []
            out[canon] = []
            for item in rows:
                u = self._url_from_gallery_or_hero_row(item)
                if u:
                    out[canon].append({"url": u})
        return out

    def _find_listing_gallery_dict(self, blobs: List[Any], page_url: str) -> Dict[str, Any]:
        """Prefer listing.gallery; many QMI pages only expose the same buckets via HeroGallery*."""
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            inner = node.get("gallery")
            if not isinstance(inner, dict):
                inner = node.get("Gallery")
            if isinstance(inner, dict) and self._listing_gallery_has_required_shape(inner):
                return inner
            if self._listing_gallery_has_required_shape(node):
                return node
        best_hero: Optional[Dict[str, Any]] = None
        best_hero_score = -1
        for node in self._iter_json(blobs):
            if not isinstance(node, dict) or not self._hero_gallery_container_has_shape(node):
                continue
            score = 0
            for _, hk in self._HERO_GALLERY_KEYS:
                v = node.get(hk)
                if isinstance(v, list):
                    score += len(v)
            if score > best_hero_score:
                best_hero_score = score
                best_hero = node
        if best_hero is not None:
            return self._canonical_gallery_from_hero_container(best_hero)
        # Some routes ship minimal RSC HTML with no listing image JSON (client-rendered gallery).
        self.logger.debug(
            "No listing.gallery or HeroGallery* in server JSON; empty image buckets: %s",
            page_url,
        )
        return {k: [] for k in self._LISTING_GALLERY_KEYS}

    def _urls_from_listing_gallery(self, gallery: Dict[str, Any]) -> Dict[str, List[str]]:
        """Only gallery.<key>[i].url — no other JSON fields or HTML sources."""
        out: Dict[str, List[str]] = {}
        for key in self._LISTING_GALLERY_KEYS:
            raw_list: List[str] = []
            arr = gallery.get(key)
            if arr is None:
                arr = []
            if isinstance(arr, list):
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url")
                    if isinstance(url, str) and url.strip():
                        raw_list.append(self._normalize_image_url(url.strip()))
            out[key] = self._sanitize_image_urls(raw_list)
        return out

    def _flatten_gallery_image_urls(self, buckets: Dict[str, List[str]]) -> List[str]:
        merged: List[str] = []
        for key in self._LISTING_GALLERY_KEYS:
            merged.extend(buckets.get(key) or [])
        return merged

    def _extract_images(self, response: scrapy.http.Response, page_json_blobs: List[Any]) -> List[str]:
        gallery = self._find_listing_gallery_dict(page_json_blobs, response.url)
        buckets = self._urls_from_listing_gallery(gallery)
        return self._flatten_gallery_image_urls(buckets)

    def _has_model_product_types_in_page(self, response: scrapy.http.Response) -> bool:
        # JSON/stream-based only (no HTML selector heuristics).
        stream_text = self._normalized_stream_text(response.text)
        if re.search(
            r'"(?:Floorplans|FloorPlans|floorplans|planCollection|PlanCollection|modelCollection|ModelCollection)"\s*:\s*\[',
            stream_text,
        ):
            return True

        blobs = self._extract_embedded_json(response) + self._extract_jsonld(response)
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                key_l = str(key).lower()
                if "floorplan" in key_l or key_l in {"plans", "plan", "models", "modelcollection", "plancollection"}:
                    if isinstance(value, list) and len(value) > 0:
                        return True
                    if isinstance(value, dict) and len(value) > 0:
                        return True
                    if isinstance(value, str) and value.strip():
                        return True
        return False

    def _sanitize_image_urls(self, urls: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for raw in urls:
            url = self._normalize_image_url(str(raw or ""))
            url = self._clean_text(url)
            if not url:
                continue
            if not re.search(r"\.(?:jpg|jpeg|png|webp)(?:\?|$)", url, re.I):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    def _normalize_image_url(self, url: str) -> str:
        parsed = urlparse(url or "")
        if parsed.path.startswith("/_next/image"):
            q = parse_qs(parsed.query or "")
            nested = (q.get("url") or [""])[0]
            if nested:
                return unquote(nested)
        return url

    def _extract_address_from_text(self, response: scrapy.http.Response) -> Optional[Dict[str, str]]:
        source = self._meta_description(response) or self._clean_text(" ".join(response.css("body *::text").getall()))
        m = re.search(
            r"(\d+[A-Za-z0-9\s\.\-#]+?)\s+in\s+([A-Za-z\s\.-]+),\s*([A-Z]{2})\b",
            source,
        )
        if not m:
            return None
        zip_match = re.search(r"\b(\d{5}(?:-\d{4})?)\b", response.text)
        return {
            "street": self._clean_text(m.group(1)),
            "city": self._clean_text(m.group(2)),
            "state": self._clean_text(m.group(3)),
            "zip": zip_match.group(1) if zip_match else "",
        }

    def _extract_community_fields(self, response: scrapy.http.Response) -> Dict[str, str]:
        stream_text = self._normalized_stream_text(response.text)
        page_text = self._clean_text(" ".join(response.css("body *::text").getall()))
        fields: Dict[str, str] = {}

        community_name = response.css("h1::text, h2::text").get()
        if community_name:
            fields["Community Name"] = self._clean_text(community_name.split(":")[0])

        desc = self._extract_overall_description_from_response(response)
        if desc:
            fields["Overall Description of the Community"] = self._clean_rich_text(desc)

        phone = self._extract_phone(response, stream_text)
        if phone:
            fields["Phone number"] = phone

        hours = self._extract_hours(stream_text, response.text)
        if hours is not None:
            fields["Model hours"] = hours

        status = self._extract_status(stream_text, "", response)
        if status:
            fields["status"] = status

        fields["latitude"] = self._extract_first_numeric_string(
            stream_text, ["Latitude", "latitude", "lat", "SalesCenterLatitude"]
        )
        fields["longitude"] = self._extract_first_numeric_string(
            stream_text, ["Longitude", "longitude", "lng", "lon", "SalesCenterLongitude"]
        )
        fields["Street Address"] = self._extract_string(stream_text, "StreetAddress") or self._extract_string(
            stream_text, "SalesCenterStreetAddress"
        )
        fields["Zip Code"] = self._extract_string(stream_text, "ZipCode") or self._extract_string(
            stream_text, "SalesCenterZipCode"
        )

        fields["Adult Community (Y/N)"] = ""

        fields["Model/Product Types Available"] = "Y" if self._has_model_product_types_in_page(response) else ""

        sqft_values = self._extract_all_numbers(stream_text, "SquareFootage")
        if sqft_values:
            fields["Minimum SQFT"] = self._number_to_str(min(sqft_values))
            fields["Maximum SQFT"] = self._number_to_str(max(sqft_values))

        garage_values = self._extract_all_numbers(stream_text, "Garages")
        if garage_values:
            fields["# Garages"] = self._number_to_str(max(garage_values))
            fields["Garages (Y/N)"] = "Y" if max(garage_values) > 0 else "N"

        story_values = self._extract_all_numbers(stream_text, "Stories")
        if story_values:
            fields["# of Floors"] = self._number_to_str(max(story_values))

        price_values = self._extract_all_numbers(stream_text, "CurrentPrice") or self._extract_all_numbers(stream_text, "BasePrice")
        if price_values:
            fields["Minimum Base Price (Current)"] = self._number_to_str(min(price_values))
            fields["Maximum Base Price/All In Price"] = self._number_to_str(max(price_values))
        page_range = self._extract_price_range_from_text(f"{stream_text} {response.text}")
        if page_range:
            range_min, range_max = page_range
            fields["Minimum Base Price (Current)"] = self._number_to_str(range_min)
            fields["Maximum Base Price/All In Price"] = self._number_to_str(range_max)

        amenity_types = self._unique_preserve_order(
            self._extract_all_strings(stream_text, "AmenityName")
        )
        if amenity_types:
            fields["Amenity Type"] = ",".join(amenity_types)
            fields["Amenities Available"] = "Y"
        else:
            fields["Amenities Available"] = "N"

        fields["Attributes/Features"] = self._lookup_value(
            self._extract_embedded_json(response) + self._extract_jsonld(response),
            [
                "Attributes",
                "Attribute",
                "Features",
                "Feature",
                "communityFeatures",
                "AttributesFeatures",
            ],
        )

        page_text_lower = page_text.lower()
        if re.search(r"\b(first|main)\s+floor\s+(master|owner)\b|\b(master|owner)\s+on\s+(the\s+)?(first|main)\s+floor\b", page_text_lower):
            fields["First Floor Master (Y/N)"] = "Y"

        return fields

    def _merge_blank_fields(self, target: Dict[str, Any], source: Dict[str, Any]) -> None:
        for key, value in source.items():
            if value and not target.get(key):
                target[key] = value

    def _finish_item(self, data: Dict[str, Any]) -> Dict[str, Any]:
        raw_status = self._clean_text(str(data.get("status", "")))
        if raw_status.lower() in {"", "null", "none", "na", "n/a"}:
            data["status"] = ""
        else:
            normalized_status = self._normalize_status(raw_status)
            data["status"] = normalized_status
        data["Adult Community (Y/N)"] = ""
        data["Street Address"] = self._clean_text(str(data.get("Street Address", "")))
        data["Amenities Available"] = "Y" if data.get("Amenity Type") else "N"
        data["Model/Product Types Available"] = (
            "Y" if str(data.get("Model/Product Types Available", "") or "").strip().upper() == "Y" else ""
        )
        data["First Floor Master (Y/N)"] = ""
        min_sqft = self._normalize_numeric_text(data.get("Minimum SQFT", ""))
        max_sqft = self._normalize_numeric_text(data.get("Maximum SQFT", ""))
        qmi_sqft = self._normalize_numeric_text(data.get("QMI Model SQFT", ""))
        if min_sqft and max_sqft and min_sqft == max_sqft:
            data["Maximum SQFT"] = ""
        if min_sqft and qmi_sqft and min_sqft == qmi_sqft:
            data["Maximum SQFT"] = ""
        self.scraped_count += 1
        self.logger.info("%d url scraped ✅", self.scraped_count)
        return data

    def _normalize_numeric_text(self, value: Any) -> str:
        txt = self._clean_text(str(value or ""))
        if not txt:
            return ""
        txt = txt.replace(",", "").replace("$", "").replace("%", "")
        try:
            num = float(txt)
        except ValueError:
            return ""
        return self._number_to_str(num)

    def _state_city_from_url(self, url: str) -> Optional[Tuple[str, str]]:
        # /new-construction-homes/<state>/<city>/...
        m = re.search(r"/new-construction-homes/([^/]+)/([^/]+)/", url)
        if not m:
            return None
        return (m.group(1), m.group(2))

    def _community_slug_from_url(self, url: str) -> str:
        parts = [p for p in url.split("/") if p]
        try:
            idx = parts.index("new-construction-homes")
            # .../state/city/<community>/<model>/
            if len(parts) >= idx + 5:
                return parts[idx + 3].replace("-", " ").title()
        except ValueError:
            return ""
        return ""

    def _model_slug_from_url(self, url: str) -> str:
        parts = [p for p in url.split("/") if p]
        if not parts:
            return ""
        last = parts[-1]
        if last in {"www.khov.com", "khov.com"}:
            return ""
        return last.replace("-", " ").title()

    def _is_qmi_page(self, url: str, qmi_block: str = "", has_quick_move_container: bool = False) -> bool:
        u = (url or "").lower()
        if any(token in u for token in ("quick-move", "quick move", "move-in", "movein", "/qmi/")):
            return True
        if has_quick_move_container:
            return True
        block = qmi_block or ""
        # Avoid classifying normal plan pages as QMI just because a generic JSON block exists.
        # Require explicit QMI markers in the extracted block.
        qmi_markers = ('"MoveInDate"', '"CurrentPrice"', '"StreetAddress"', '"ZipCode"')
        return all(marker in block for marker in qmi_markers)

    def _has_quick_move_container(self, response: scrapy.http.Response) -> bool:
        return bool(response.xpath("//div[contains(@class,'QuickMoveInContainer')]").get())

    def _first_qmi_record_from_json(self, blobs: List[Any]) -> Dict[str, Any]:
        # Prefer first home payload that contains PageTitle.
        for node in self._iter_json(blobs):
            if isinstance(node, dict) and isinstance(node.get("PageTitle"), str) and node.get("PageTitle", "").strip():
                return node
        required_keys = {"CurrentPrice", "MoveInDate", "StreetAddress", "City", "State", "ZipCode", "modelName", "model_name"}
        best: Dict[str, Any] = {}
        best_score = -1
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            score = sum(1 for k in required_keys if k in node)
            if score > best_score:
                best_score = score
                best = node
            if score >= 3:
                return node
        return best if best_score > 0 else {}

    def _extract_model_name_from_heading(self, response: scrapy.http.Response) -> str:
        heading = self._clean_text(response.css("h1::text, h2::text").get("") or "")
        if not heading:
            return ""
        heading = re.split(r"\s*[:|\-]\s*", heading, maxsplit=1)[0].strip()
        return heading

    def _extract_infoblock_description(self, response: scrapy.http.Response) -> str:
        texts = [self._clean_text(t) for t in response.css("div.InfoBlock *::text").getall()]
        texts = [t for t in texts if t]
        if not texts:
            return ""
        joined = self._clean_text(" ".join(texts))
        return joined

    def _extract_overall_description_from_response(self, response: scrapy.http.Response) -> str:
        candidates: List[str] = []
        info_block = self._extract_infoblock_description(response)
        if info_block:
            candidates.append(info_block)
        selectors = (
            "section[class*='description'] *::text",
            "div[class*='description'] *::text",
            "section[class*='overview'] *::text",
            "div[class*='overview'] *::text",
        )
        for selector in selectors:
            txt = self._clean_text(" ".join(response.css(selector).getall()))
            if txt:
                candidates.append(txt)
        filtered = [t for t in candidates if len(t) >= 40 and len(re.findall(r"[A-Za-z]{2,}", t)) >= 6]
        if not filtered:
            return ""
        return max(filtered, key=len)

    def _community_details_url(self, url: str) -> str:
        # remove last segment (model/QMI) -> community page
        if not url.endswith("/"):
            url = url + "/"
        parts = url.rstrip("/").split("/")
        if len(parts) <= 4:
            return url
        return "/".join(parts[:-1]) + "/"

    def _safe_json_loads(self, s: str) -> Optional[Any]:
        s = (s or "").strip()
        if not s:
            return None
        # Sometimes JSON-LD contains multiple objects without being a list; try best-effort
        try:
            return json.loads(s)
        except Exception:
            return None

    def _extract_first_json_object(self, text: str) -> str:
        # naive brace matching to pull first {...}
        start = text.find("{")
        if start == -1:
            return ""
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return ""

    def _iter_json(self, blobs: List[Any]) -> Iterable[Any]:
        for b in blobs:
            yield from self._walk_json(b)

    def _walk_json(self, node: Any) -> Iterable[Any]:
        yield node
        if isinstance(node, dict):
            for v in node.values():
                yield from self._walk_json(v)
        elif isinstance(node, list):
            for v in node:
                yield from self._walk_json(v)

    def _first_str_from_json(self, blobs: List[Any], keys: set) -> str:
        for node in self._iter_json(blobs):
            if isinstance(node, dict):
                for k in keys:
                    v = node.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        return ""

    def _collect_strings_from_json(self, blobs: List[Any], keys: set) -> List[str]:
        out: List[str] = []
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            for k in keys:
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(self._clean_text(v))
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.strip():
                            out.append(self._clean_text(item))
        return [v for v in self._unique_preserve_order(out) if v]

    def _first_number_from_json(self, blobs: List[Any], keys: set) -> Optional[float]:
        for node in self._iter_json(blobs):
            if isinstance(node, dict):
                for k in keys:
                    v = node.get(k)
                    if isinstance(v, (int, float)):
                        return v
                    if isinstance(v, str):
                        vv = v.strip().replace(",", "")
                        if re.fullmatch(r"\d+(\.\d+)?", vv):
                            try:
                                return float(vv)
                            except Exception:
                                pass
        return None

    def _collect_numbers_from_json(self, blobs: List[Any], keys: set) -> List[float]:
        out: List[float] = []
        wanted = {k.lower() for k in keys}
        for node in self._iter_json(blobs):
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                if str(key).lower() not in wanted:
                    continue
                if isinstance(value, (int, float)):
                    out.append(float(value))
                elif isinstance(value, str):
                    vv = value.strip().replace(",", "").replace("$", "")
                    if re.fullmatch(r"-?\d+(\.\d+)?", vv):
                        try:
                            out.append(float(vv))
                        except Exception:
                            pass
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, (int, float)):
                            out.append(float(item))
                        elif isinstance(item, str):
                            vv = item.strip().replace(",", "").replace("$", "")
                            if re.fullmatch(r"-?\d+(\.\d+)?", vv):
                                try:
                                    out.append(float(vv))
                                except Exception:
                                    pass
        return out

    def _clean_text(self, s: str) -> str:
        s = re.sub(r"\s+", " ", (s or "")).strip()
        return s

    def _decode_unicode_escaped_html(self, value: str) -> str:
        """Decode JSON/unicode-escaped HTML into a plain single-line text string."""
        txt = str(value or "")
        if not txt:
            return ""

        # Handle JSON-style escaped payloads like "\\u003cp\\u003eHello\\u003c/p\\u003e".
        for _ in range(2):
            try:
                txt = json.loads(f'"{txt}"')
            except Exception:
                break

        # Fallback replacements for partially escaped inputs.
        txt = (
            txt.replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0026", "&")
            .replace("\\n", " ")
            .replace("\\/", "/")
        )
        txt = html.unescape(txt)
        txt = re.sub(r"<[^>]+>", " ", txt)
        return self._clean_text(txt)

    def _clean_rich_text(self, s: str) -> str:
        return self._decode_unicode_escaped_html(s or "")

    def _format_full_street_address(self, street: Any, city: Any, state: Any, zip_code: Any) -> str:
        street_txt = self._clean_text(str(street or ""))
        city_txt = self._clean_text(str(city or ""))
        state_txt = self._clean_text(str(state or ""))
        zip_txt = self._clean_text(str(zip_code or ""))
        if not any((street_txt, city_txt, state_txt, zip_txt)):
            return ""
        city_state = ""
        if city_txt and state_txt:
            city_state = f"{city_txt}, {state_txt}"
        else:
            city_state = city_txt or state_txt
        city_state_zip = f"{city_state} {zip_txt}".strip() if zip_txt else city_state
        if street_txt and city_state_zip:
            return f"{street_txt}, {city_state_zip}"
        return street_txt or city_state_zip

    def _number_to_str(self, value: float) -> str:
        if float(value).is_integer():
            return str(int(value))
        return str(value)


