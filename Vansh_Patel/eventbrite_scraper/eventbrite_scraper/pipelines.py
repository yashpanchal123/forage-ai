from copy import deepcopy

from itemadapter import ItemAdapter


class EventbriteFlexiblePipeline:
    """
    Generic cleanup pipeline designed to be easy to adjust later.

    - Trims strings recursively.
    - Collapses duplicate whitespace in strings.
    - Preserves None/empty values to keep stable output schema.
    """

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        data = adapter.asdict() if hasattr(adapter, "asdict") else dict(item)
        normalized = self._ensure_event_schema(data)
        return self._clean(deepcopy(normalized))

    def _ensure_event_schema(self, data):
        data.setdefault("provider", None)
        data.setdefault("module", None)
        data.setdefault("groupId", None)
        data.setdefault("id", None)
        data.setdefault("createdAt", None)
        data.setdefault("updatedAt", None)
        data.setdefault("title", None)
        data.setdefault("source", {})
        data.setdefault("recordSource", {})
        data.setdefault("location", {})
        data.setdefault("siteId", None)
        data.setdefault("isonline", False)
        data.setdefault("metadata", {})

        source = data["source"]
        if isinstance(source, dict):
            source.setdefault("name", None)
            source.setdefault("id", None)
            source.setdefault("url", None)

        record_source = data["recordSource"]
        if isinstance(record_source, dict):
            record_source.setdefault("id", None)
            record_source.setdefault("url", None)

        location = data["location"]
        if isinstance(location, dict):
            location.setdefault("name", None)
            location.setdefault("address", None)
            location.setdefault("latitude", None)
            location.setdefault("longitude", None)
            location.setdefault("nearBy", None)

        metadata = data["metadata"]
        if isinstance(metadata, dict):
            metadata.setdefault("event", {})

        event = metadata.get("event", {}) if isinstance(metadata, dict) else {}
        if isinstance(event, dict):
            event.setdefault("description", None)
            event.setdefault("eventRoles", [])
            event.setdefault("eventSchedule", {})
            event.setdefault("status", None)
            event.setdefault("eventPricing", {})
            event.setdefault("availability", None)
            event.setdefault("purchaseUrl", None)
            event.setdefault("audiences", [])
            event.setdefault("categories", [])
            event.setdefault("tags", [])
            event.setdefault("media", [])
            metadata["event"] = event

        return data

    def _clean(self, value):
        if isinstance(value, dict):
            output = {}
            for key, val in value.items():
                output[key] = self._clean(val)
            return output

        if isinstance(value, list):
            output = []
            for val in value:
                output.append(self._clean(val))
            return output

        if isinstance(value, str):
            compact = " ".join(value.split()).strip()
            return compact

        return value
