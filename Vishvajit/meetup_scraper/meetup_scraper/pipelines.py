# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
from itemadapter import ItemAdapter


class CleanEmptyPipeline:

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)

        for field, value in list(adapter.items()):
            adapter[field] = self.clean(value)

        return item

    def clean(self, obj):
        if isinstance(obj, dict):
            return {k: self.clean(v) for k, v in obj.items()}

        elif isinstance(obj, list):
            return [self.clean(v) for v in obj]

        elif isinstance(obj, str) and obj.strip() == "":
            return None

        else:
            return obj