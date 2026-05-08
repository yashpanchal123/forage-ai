"""GraphQL documents for Mainz Sitepark search API."""

# Reliable listing fields (first Solr row can break ``teaser`` resolution on the server).
LISTING_SEARCH_QUERY_MINIMAL = """
query ListingSearch($searchInput: SearchInput!) {
  search(input: $searchInput) {
    total
    offset
    limit
    results {
      id
      objectType
      name
      location
    }
  }
}
"""

# ContactTeaser.contactData — use together with LISTING_SEARCH_QUERY_MINIMAL; see spider paging notes.
LISTING_SEARCH_QUERY_CONTACT = """
query ListingSearch($searchInput: SearchInput!) {
  search(input: $searchInput) {
    total
    offset
    limit
    results {
      id
      objectType
      name
      location
      teaser {
        __typename
        ... on ContactTeaser {
          contactData {
            emails {
              email
            }
            phones {
              nationalNumber
              internationalNumber
              uri
            }
          }
        }
      }
    }
  }
}
"""
