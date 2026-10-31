# websearch fixtures

Frozen response bodies in each vendor's documented shape (read 2026-10-30), for the offline
`tests/unit/test_websearch.py`. No request headers, no keys: the bodies are what a vendor sends
back, written by hand from the field lists and examples in the official API references:

- `brave/web_search.json` — Brave Search API `GET /res/v1/web/search` (`web.results[]`: `title`,
  `url`, `description`, `age`, `page_age`, `language`).
- `tavily/search.json`, `tavily/extract.json`, `tavily/extract_failed.json` — Tavily
  `POST /search` and `POST /extract` (`results[]`, `failed_results[]`, `response_time`, `usage`,
  `request_id`).
- `firecrawl/search.json`, `firecrawl/scrape.json`, `firecrawl/scrape_403.json` — Firecrawl v2
  `POST /v2/search` (`data.web[]` with `position`) and `POST /v2/scrape` (`data.markdown`,
  `data.metadata` with `sourceURL`, `url`, `statusCode`, `contentType`).
- `direct/article.html` — a plain news page for the direct fetcher's HTML-to-text.

The pages and URLs are invented; nothing here was fetched.
