
import os
import time
import asyncio
import re
import json
from typing import Any
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, HTTPException, Query
from app.crypto import router as crypto_router
from app.bybit import router as bybit_router
from app.market_context import router as market_context_router
from app.paper_trading import router as paper_router, start_paper_worker, stop_paper_worker
from app.exchange_scanners import router as exchange_router
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run virtual trading on the server, independently of the Android app.
    start_paper_worker()
    try:
        yield
    finally:
        await stop_paper_worker()


app = FastAPI(
    title="HAN INVEST API",
    description="Market research and investment analysis service",
    version="0.7.0",
    lifespan=lifespan,
)

app.include_router(crypto_router)
app.include_router(bybit_router)
app.include_router(market_context_router)
app.include_router(paper_router)
app.include_router(exchange_router)

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
NEWS_CACHE_TTL = int(os.getenv("NEWS_CACHE_TTL", "3600"))
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "25"))

_cache: dict[str, tuple[float, Any]] = {}

FINANCE_TERMS = (
    "stock", "stocks", "share", "shares", "earnings",
    "revenue", "profit", "loss", "forecast", "analyst",
    "valuation", "market", "investor", "investment",
    "dividend", "buyback", "upgrade", "downgrade",
    "merger", "acquisition", "lawsuit", "regulation",
    "tariff", "supply", "demand", "quarter",
    "guidance", "nasdaq", "nyse", "sec filing",
    "ceo", "financial results", "trading",
    "business", "company", "sales", "growth"
)


def get_cache(key: str, ttl: int):
    cached = _cache.get(key)

    if cached and time.time() - cached[0] < ttl:
        return cached[1]

    return None


def save_cache(key: str, data: Any):
    _cache[key] = (time.time(), data)


async def fetch_json(
    url: str,
    headers: dict | None = None,
    retries: int = 2
):
    request_headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/json",
    }

    if headers:
        request_headers.update(headers)

    last_error = "External provider returned an invalid response."

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.get(
                    url,
                    headers=request_headers
                )

                if response.status_code == 429:
                    last_error = "Provider rate limit reached."

                    if attempt < retries:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue

                    raise HTTPException(
                        status_code=503,
                        detail=last_error
                    )

                response.raise_for_status()

                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    last_error = "Provider returned empty or invalid JSON."

                    if attempt < retries:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue

                    raise HTTPException(
                        status_code=503,
                        detail=last_error
                    )

                if not isinstance(data, dict):
                    raise HTTPException(
                        status_code=503,
                        detail="Provider returned an unexpected data format."
                    )

                return data

        except HTTPException:
            raise

        except httpx.HTTPError:
            last_error = "External provider is temporarily unavailable."

            if attempt < retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue

            raise HTTPException(
                status_code=503,
                detail=last_error
            )

    raise HTTPException(status_code=503, detail=last_error)


async def cached_json(
    key: str,
    url: str,
    headers: dict | None = None,
    ttl: int | None = None
):
    cache_ttl = ttl if ttl is not None else CACHE_TTL
    cached = get_cache(key, cache_ttl)

    if cached is not None:
        return cached

    data = await fetch_json(url, headers)
    save_cache(key, data)

    return data


async def resolve_company(query: str):
    clean = query.strip()
    upper = clean.upper()

    if upper.isalpha() and len(upper) <= 5:
        try:
            data = await cached_json(
                "sec_universe",
                "https://www.sec.gov/files/company_tickers.json",
            )

            for item in data.values():
                if item["ticker"].upper() == upper:
                    return {
                        "ticker": upper,
                        "name": item["title"],
                    }

        except (HTTPException, KeyError, TypeError):
            return {
                "ticker": upper,
                "name": clean,
            }

    return {
        "ticker": None,
        "name": clean,
    }


def company_matches_title(title: str, company: dict) -> bool:
    title_lower = title.lower()
    name = company["name"].strip()

    aliases = [name]

    known_aliases = {
        "Apple Inc.": ["Apple", "Apple Inc"],
        "NVIDIA Corporation": ["NVIDIA", "Nvidia Corporation"],
        "Microsoft Corporation": ["Microsoft"],
        "Amazon.com, Inc.": ["Amazon"],
        "Alphabet Inc.": ["Alphabet", "Google"],
        "Meta Platforms, Inc.": ["Meta", "Facebook"],
        "Tesla, Inc.": ["Tesla"],
    }

    aliases.extend(known_aliases.get(name, []))

    ticker = company.get("ticker")
    if ticker:
        aliases.append(ticker)

    for alias in aliases:
        alias = alias.strip().lower()

        if not alias:
            continue

        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"

        if re.search(pattern, title_lower):
            return True

    return False


@app.on_event("startup")
async def startup_paper_worker():
    start_paper_worker()


@app.on_event("shutdown")
async def shutdown_paper_worker():
    await stop_paper_worker()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "HAN INVEST API",
        "version": "0.6.0"
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
    company = await resolve_company(q)

    company_name = company["name"]
    ticker = company.get("ticker")

    cache_key = f"news:v3:{company_name.lower()}"

    cached = get_cache(cache_key, NEWS_CACHE_TTL)

    if cached is not None:
        return cached

    finance_query = (
        f'"{company_name}" AND '
        '(stock OR shares OR earnings OR revenue OR profit '
        'OR forecast OR analyst OR valuation OR dividend '
        'OR buyback OR merger OR acquisition OR lawsuit '
        'OR regulation OR tariff OR financial results '
        'OR investor OR trading OR business OR company)'
    )

    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={quote_plus(finance_query)}"
        "&mode=ArtList&format=json"
        "&sort=DateDesc&maxrecords=50"
    )

    try:
        data = await fetch_json(url, retries=2)

    except HTTPException:
        return {
            "query": q,
            "company": company_name,
            "ticker": ticker,
            "source": "GDELT",
            "count": 0,
            "articles": [],
            "status": "temporarily_unavailable",
            "message": (
                "Новостной источник временно недоступен "
                "или вернул некорректный ответ. Попробуйте позже."
            )
        }

    articles = data.get("articles", [])

    if not isinstance(articles, list):
        articles = []

    relevant_articles = []

    for article in articles:
        if not isinstance(article, dict):
            continue

        title = article.get("title") or ""

        if company_matches_title(title, company):
            relevant_articles.append(article)

    result = {
        "query": q,
        "company": company_name,
        "ticker": ticker,
        "source": "GDELT",
        "count": len(relevant_articles),
        "articles": relevant_articles[:20],
        "status": "ok"
    }

    save_cache(cache_key, result)

    return result


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
