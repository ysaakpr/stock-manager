# Curated RSS fixtures (M6.1)

Frozen real RSS payloads, fetched live on **2026-09-02** with a browser User-Agent. Tests parse
these; the suite never touches the network (AGENTIC_CONTEXT §8 / B8). Both publishers are
Government of India bodies; only headlines and links are read into L1 (§4.1 license note).

| File | Source URL | Bytes | sha256 |
|---|---|---|---|
| `rbi/2026-09-02/pressreleases_rss.xml` | `https://www.rbi.org.in/pressreleases_rss.xml` | 120049 | `4c183fb7471d4ab99a3231530f6f00b39c3417cdbd0f1c4c789b72682cc2f118` |
| `pib/2026-09-02/RssMain.xml` | `https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3` | 8010 | `5b9a9db7cf1e9e7c404a29dc2836601d4a5cd76c3893cb0d4ac3f8d7363bdcc7` |

- **RBI** (the active feed): RSS 2.0, 10 `<item>`s, each with `<title>` (CDATA), `<link>` and
  `<pubDate>` (RFC 822 without a zone, e.g. `Wed, 02 Sep 2026 11:30:00` → read as Asia/Kolkata).
  Each item also carries a `<description>` holding the full press release — deliberately **not**
  read into L1, which is what the "no article bodies" test proves against this file.
- **PIB**: RSS 2.0, 20 `<item>`s, each with `<title>` and `<link>` **only** — no `<pubDate>`, no
  `<dc:date>`, and no channel date. It is the fixture for the "a feed with no source timestamp
  fails loud rather than being dated from the wall clock" test, and is why PIB is `active: false`
  in `rss_feeds.yaml`. (This capture is the Hindi variant the endpoint redirected to; the shape —
  title + link, no dates — is identical across languages and is what the test asserts.)
