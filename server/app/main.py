
import os
import time
from typing import Any
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="HAN INVEST API",
    description="Market research and investment analysis service",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "HAN INVEST contact@example.com"
)
CACHE_TTL = int(os.getenv("CACHE_TTL", "900"))
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))

_cache: dict[str, tuple[float, Any]] = {}

FINANCE_TERMS = (
    "stock", "stocks", "share", "shares", "earnings",
    "revenue", "profit", "loss", "forecast", "analyst",
    "valuation", "market", "investor", "investment",
    "dividend", "buyback", "upgrade", "downgrade",
    "merger", "acquisition", "lawsuit", "regulation",
    "tariff", "supply", "demand", "quarter",
    "guidance", "nasdaq", "nyse", "sec filing",
    "ceo", "financial results", "trading"
)


async def fetch_json(url: str, headers: dict | None = None):
    request_headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/json",
    }

    if headers:
        request_headers.update(headers)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                url,
                headers=request_headers
            )
            response.raise_for_status()
            return response.json()

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"External provider error: {str(exc)}",
        )


async def cached_json(
    key: str,
    url: str,
    headers: dict | None = None
):
    now = time.time()
    cached = _cache.get(key)

    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    data = await fetch_json(url, headers)
    _cache[key] = (now, data)

    return data


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "HAN INVEST API"
    }


@app.get("/api/v1/universe")
async def universe():
    url = "https://www.sec.gov/files/company_tickers.json"

    data = await cached_json("sec_universe", url)

    companies = [
        {
            "cik": str(item["cik_str"]).zfill(10),
            "ticker": item["ticker"],
            "name": item["title"],
        }
        for item in data.values()
    ]

    return {
        "count": len(companies),
        "source": "SEC EDGAR",
        "companies": companies,
    }


@app.get("/api/v1/news")
async def news(
    q: str = Query(..., min_length=2, max_length=100)
):
    finance_query = (
        f'"{q}" AND '
        '(stock OR shares OR earnings OR revenue OR profit '
        'OR forecast OR analyst OR valuation OR dividend '
        'OR buyback OR merger OR acquisition OR lawsuit '
        'OR regulation OR tariff OR financial results '
        'OR investor OR trading)'
    )

    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={quote_plus(finance_query)}"
        "&mode=ArtList&format=json"
        "&sort=DateDesc&maxrecords=50"
    )

    data = await cached_json(
        f"news:finance:{q.lower()}",
        url
    )

    articles = data.get("articles", [])

    relevant_articles = []

    for article in articles:
        title = (article.get("title") or "").lower()

        if any(term in title for term in FINANCE_TERMS):
            relevant_articles.append(article)

    return {
        "query": q,
        "source": "GDELT",
        "count": len(relevant_articles),
        "articles": relevant_articles,
    }


@app.get("/api/v1/sec/{ticker}/filings")
async def sec_filings(ticker: str):
    ticker = ticker.upper().strip()

    universe_data = await cached_json(
        "sec_universe",
        "https://www.sec.gov/files/company_tickers.json",
    )

    company = next(
        (
            item for item in universe_data.values()
            if item["ticker"].upper() == ticker
        ),
        None,
    )

    if not company:
        raise HTTPException(
            status_code=404,
            detail="Ticker not found"
        )

    cik = str(company["cik_str"]).zfill(10)

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"

    data = await cached_json(
        f"filings:{cik}",
        url
    )

    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    documents = recent.get("primaryDocument", [])

    filings = []

    for i in range(min(len(forms), 30)):
        accession = accessions[i].replace("-", "")
        document = documents[i]

        filings.append({
            "form": forms[i],
            "date": dates[i],
            "document": document,
            "url": (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{accession}/{document}"
            ),
        })

    return {
        "ticker": ticker,
        "company": data.get("name"),
        "source": "SEC EDGAR",
        "filings": filings,
    }
