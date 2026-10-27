# Desktop Research Search

`GET /desktop/research/search` runs one query across the research sources and returns each source's answer on its own. The desktop Search tab is its consumer.

- **Local only.** Served on the local server (`http://127.0.0.1:7878` by default). The shared check in `meridian/routes/_local_only.py` gives `403` unless the caller is on loopback, no `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host` or `X-Real-IP` header is sent, the `Host` header names `localhost`, `127.0.0.1` or `[::1]` (with an optional port, which stops DNS rebinding), `Sec-Fetch-Site` is `none` or `same-origin` when sent, and `Origin` is an http(s) loopback origin when sent. On the hosted service, anonymous callers get `401` from the app-wide gate.
- **Read-only.** Nothing is written to the database and nothing is captured. No token or header value is returned.

## Request

| Parameter | Required | Default | Notes |
|---|---|---|---|
| `q` | yes | none | 1 to 300 characters. Otherwise `422` with a plain message. |
| `sources` | no | `openalex,semantic_scholar,arxiv,github_repos` | Comma list of `openalex`, `semantic_scholar`, `arxiv`, `github_repos`, `github_code`. An unknown name returns `ok: false` with reason `unknown source`. |
| `limit` | no | `10` | Results per source. Capped at `25`. |

```
GET /desktop/research/search?q=protein%20folding&sources=openalex,arxiv&limit=5
```

## Response

```json
{
  "query": "protein folding",
  "sources": {
    "openalex": {
      "ok": true,
      "reason": "",
      "results": [
        {
          "id": "W1234567890",
          "title": "Example paper",
          "authors": ["A. Author"],
          "year": 2024,
          "url": "https://example.org/paper",
          "source": "openalex",
          "abstract": "At most 400 characters."
        }
      ]
    },
    "github_code": {
      "ok": false,
      "reason": "GitHub rate limit reached for public search; try again in a minute",
      "results": []
    }
  }
}
```

Each result has exactly `id`, `title`, `authors`, `year` (an integer or `null`), `url` (https), `source` and `abstract`.

## Behavior

- A source that fails returns `ok: false` with one plain `reason`. The other sources still return. Raw exception text and upstream URLs are never echoed.
- `arxiv` falls back to OpenAlex, then Semantic Scholar, when arXiv refuses or cannot be reached from the server. `ok` stays `true` and `reason` says where the results came from.
- GitHub sources work without a token (public search). A rate limit is reported as a plain reason.
- Each source has a 45-second limit, so one slow upstream cannot hold the tab open.
