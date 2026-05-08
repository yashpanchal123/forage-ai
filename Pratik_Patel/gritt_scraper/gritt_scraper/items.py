import scrapy

# One object in profile_data.json / each CSV row — keys must match pipeline output.
# Spider yields nested GrittInvestorProfileItem; pipelines.iter_gritt_profile_flat_rows
# maps API-shaped data to this flat schema.
GRITT_PROFILE_EXPORT_FIELDS: tuple[str, ...] = (
    "investor_id",
    "investor_name",
    "investor_profile_url",
    "investor_location",
    "investor_description",
    "firm_name",
    "firm_role",
    "portfolio_role",
    "portfolio_company_name",
    "portfolio_company_website",
    "portfolio_linkedin",
    "portfolio_description",
    "portfolio_location",
    "investment_date",
    "investment_stage",
    "portfolio_gritt_link",
)


class GrittInvestorItem(scrapy.Item):
    public_id = scrapy.Field()
    investor_url = scrapy.Field()
    page = scrapy.Field()


class GrittInvestorProfileItem(scrapy.Item):
    """
    Spider-only shape: gritt_source_url + nested profile_data from the API.

    profile_data.json uses the flat schema GRITT_PROFILE_EXPORT_FIELDS (built in pipelines).
    """

    gritt_source_url = scrapy.Field()
    profile_data = scrapy.Field()