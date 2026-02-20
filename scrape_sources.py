import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

# Optional: YAKE keywords (if installed)
try:
    import yake  # type: ignore
except Exception:
    yake = None

# Optional: spaCy entities (only works if spacy + model are installed)
try:
    import spacy  # type: ignore
except Exception:
    spacy = None

# ✅ Playwright (needed for dynamic widgets/search pages)
try:
    from playwright.sync_api import sync_playwright  # type: ignore
except Exception:
    sync_playwright = None

# ✅ Safe import (works from root or from /scraper)
try:
    from scraper.sources import SOURCES
except Exception:
    from sources import SOURCES  # type: ignore


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

STOPWORDS = set(
    "the a an and or but if then else for to of in on at by from with without "
    "this that these those is are was were be been being as it its into "
    "can may must should will would could about over under than also "
    "such more most less very not no yes you your we our they their i me my "
    "them us".split()
)

NOISE_IDENTIFIERS = (
    "breadcrumb", "sidebar", "menu", "nav", "footer", "header",
    "social", "share", "cookie", "banner", "alert", "promo",
    "related", "subscribe", "signup", "modal"
)

DEFAULT_TIMEOUT = 45
DEFAULT_SLEEP = 0.25


@dataclass
class PageResult:
    url: str
    title: str
    main_text: str
    summary: str
    key_points: List[str]
    entities: Dict[str, List[str]]
    keywords: List[str]
    extracted_links: List[str]
    announcements: List[Dict[str, str]]


# -------------------------
# Encoding cleanup helpers
# -------------------------
def _clean_text(s: str) -> str:
    s = s or ""
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fix_mojibake(s: str) -> str:
    """Fix common “CBP�s” / smart quotes issues (best effort)."""
    if not s:
        return s
    if "�" in s:
        try:
            return s.encode("cp1252", errors="ignore").decode("utf-8", errors="ignore")
        except Exception:
            return s
    return s


# -------------------------
# HTTP session w/ retries
# -------------------------
_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is not None:
        return _SESSION

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    try:
        from urllib3.util.retry import Retry  # type: ignore
        from requests.adapters import HTTPAdapter

        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
    except Exception:
        pass

    _SESSION = s
    return s


# -------------------------
# Fetchers
# -------------------------
def _fetch_html_requests(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    sess = _get_session()
    r = sess.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()

    # Usually best to trust requests’ decoding unless it's clearly wrong.
    text = r.text
    return _fix_mojibake(text)


def _fetch_html_playwright_wait(url: str, wait_for: Optional[str] = None, timeout_ms: int = 60000) -> str:
    """Render with Playwright (system Chrome channel)."""
    if sync_playwright is None:
        raise RuntimeError("Playwright is not available. Install: pip install playwright")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        if wait_for:
            page.wait_for_selector(wait_for, timeout=timeout_ms)

        html = page.content()
        browser.close()
        return html


# -------------------------
# Generic extraction
# -------------------------
def _remove_noise(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(["script", "style", "noscript", "svg", "canvas"]):
        try:
            tag.decompose()
        except Exception:
            pass

    for tag in soup.find_all(["header", "footer", "nav", "aside"]):
        try:
            tag.decompose()
        except Exception:
            pass


def _extract_title(soup: BeautifulSoup) -> str:
    try:
        if soup.title:
            t = soup.title.get_text(strip=True)
            if t:
                return _clean_text(t)
    except Exception:
        pass

    try:
        h1 = soup.find("h1")
        if h1:
            t = h1.get_text(" ", strip=True)
            if t:
                return _clean_text(t)
    except Exception:
        pass

    return ""


def _strip_by_identifiers(container: Optional[BeautifulSoup]) -> None:
    if container is None:
        return

    for tag in list(container.find_all(True)):
        try:
            tag_id = tag.get("id") or ""
        except Exception:
            tag_id = ""
        try:
            tag_class = tag.get("class") or []
        except Exception:
            tag_class = []
        if not isinstance(tag_class, list):
            tag_class = [str(tag_class)]

        ident = f"{tag_id} {' '.join([str(c) for c in tag_class])}".lower()
        if any(k in ident for k in NOISE_IDENTIFIERS):
            try:
                tag.decompose()
            except Exception:
                pass


def _best_main_container(soup: BeautifulSoup) -> BeautifulSoup:
    for sel in ["article", "main", "#main-content", "div#main-content", "div[role=main]"]:
        try:
            node = soup.select_one(sel)
        except Exception:
            node = None
        if node:
            try:
                txt = node.get_text(" ", strip=True)
            except Exception:
                txt = ""
            if txt and len(txt) > 400:
                return node
    return soup.body if soup.body else soup


def _extract_links(container: Optional[BeautifulSoup], base_url: str, limit: int = 80) -> List[str]:
    if container is None:
        return []

    links: List[str] = []
    for a in container.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue

        abs_url = urljoin(base_url, href)
        if abs_url.startswith("http"):
            links.append(abs_url)
        if len(links) >= limit:
            break

    seen = set()
    out = []
    for x in links:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _extract_main_text_and_links(html: str, url: str) -> Tuple[str, str, List[str]]:
    soup = BeautifulSoup(html, "html.parser")
    _remove_noise(soup)

    title = _extract_title(soup)
    container = _best_main_container(soup)
    _strip_by_identifiers(container)

    try:
        main_text = container.get_text(" ", strip=True)
    except Exception:
        main_text = soup.get_text(" ", strip=True)

    main_text = _fix_mojibake(_clean_text(main_text))
    links = _extract_links(container, base_url=url, limit=80)
    return title, main_text, links


# -------------------------
# Summarization + keywords + entities (offline)
# -------------------------
def _split_sentences(text: str) -> List[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _basic_keywords(text: str, top_k: int = 12) -> List[str]:
    if not text:
        return []

    if yake is not None:
        try:
            kw_extractor = yake.KeywordExtractor(lan="en", n=1, top=top_k)
            kws = kw_extractor.extract_keywords(text)
            kws.sort(key=lambda x: x[1])
            return [k for k, _ in kws][:top_k]
        except Exception:
            pass

    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())
    freq: Dict[str, int] = {}
    for w in words:
        if w in STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1

    return [w for w, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top_k]]


def _extractive_summary(text: str, keywords: List[str], max_sentences: int = 6) -> Tuple[str, List[str]]:
    sentences = _split_sentences(text)
    if not sentences:
        return "", []

    keyset = set((k or "").lower() for k in keywords)
    scored = []
    for idx, s in enumerate(sentences[:250]):
        s_low = s.lower()
        score = 0
        for k in keyset:
            if k and k in s_low:
                score += 2
        score += max(0.0, 200.0 - abs(len(s) - 180.0)) / 200.0
        scored.append((score, idx, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    picks = sorted(scored[:max_sentences], key=lambda x: x[1])
    key_points = [p[2] for p in picks]
    summary = " ".join(key_points[:3]).strip()
    return summary, key_points


def _regex_entities(text: str) -> Dict[str, List[str]]:
    candidates = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4})\b", text or "")
    candidates = [c.strip() for c in candidates if len(c) >= 4]

    org_markers = (
        "Department", "Agency", "Administration", "Commission",
        "Office", "U.S", "United States", "CBP", "White House", "Federal Register"
    )

    orgs, other = set(), set()
    for c in candidates[:1200]:
        if any(m in c for m in org_markers):
            orgs.add(c)
        else:
            other.add(c)

    return {
        "orgs": sorted(orgs)[:20],
        "people": [],
        "locations": [],
        "other": sorted(other)[:25],
    }


_NLP = None


def _spacy_entities(text: str) -> Optional[Dict[str, List[str]]]:
    global _NLP
    if spacy is None:
        return None
    if _NLP is None:
        try:
            _NLP = spacy.load("en_core_web_sm")
        except Exception:
            _NLP = None
            return None

    doc = _NLP((text or "")[:200000])
    orgs, people, locs, misc = set(), set(), set(), set()

    for ent in doc.ents:
        if ent.label_ == "ORG":
            orgs.add(ent.text)
        elif ent.label_ == "PERSON":
            people.add(ent.text)
        elif ent.label_ in ("GPE", "LOC"):
            locs.add(ent.text)
        else:
            misc.add(ent.text)

    return {
        "orgs": sorted(orgs)[:20],
        "people": sorted(people)[:20],
        "locations": sorted(locs)[:20],
        "other": sorted(misc)[:20],
    }


# -------------------------
# Markdown table helpers
# -------------------------
def _as_markdown_table_date_announcement(items: List[Dict[str, str]]) -> str:
    lines = ["| Date | Announcement |", "|---|---|"]
    for it in items:
        d = _clean_text(it.get("date", ""))
        t = _clean_text(it.get("announcement", ""))
        lines.append(f"| {d} | {t} |")
    return "\n".join(lines)


# -------------------------
# Source detection
# -------------------------
def _is_cbp_csms(url: str) -> bool:
    return "cbp.gov/trade/automated/cargo-systems-messaging-service" in (url or "").lower()


def _is_whitehouse_fact_sheets(url: str) -> bool:
    u = (url or "").lower().rstrip("/")
    return u == "https://www.whitehouse.gov/fact-sheets"


def _is_cbp_documents_library(url: str) -> bool:
    return (url or "").lower().rstrip("/") == "https://www.cbp.gov/documents-library"


def _is_federalregister_search(url: str) -> bool:
    u = (url or "").lower()
    return "federalregister.gov" in u and ("/documents/search" in u or "document-search" in u)


# -------------------------
# 1) CBP CSMS (widget list)
# -------------------------
def _fetch_cbp_csms_html(timeout_ms: int = 60000) -> str:
    url = "https://www.cbp.gov/trade/automated/cargo-systems-messaging-service"
    # best effort: Playwright first
    try:
        return _fetch_html_playwright_wait(url, wait_for="div.gdw_content div.gdw_story", timeout_ms=timeout_ms)
    except Exception:
        return _fetch_html_requests(url)


def _parse_cbp_csms_announcements(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    _remove_noise(soup)

    items: List[Dict[str, str]] = []

    for story in soup.select("div.gdw_content div.gdw_story"):
        a = story.select_one("div.gdw_story_title a[href]")
        if not a:
            continue

        title = _clean_text(a.get_text(" ", strip=True))
        link = _clean_text(a.get("href") or "")

        pub = ""
        d = story.select_one("li.pub_date")
        if d:
            pub = _clean_text(d.get_text(" ", strip=True))

        csms_no = ""
        m = re.search(r"CSMS\s*#\s*([0-9]+)", title, re.IGNORECASE)
        if m:
            csms_no = m.group(1)

        items.append({"csms_number": csms_no, "title": title, "published": pub, "link": link})

    return items


# -------------------------
# 2) WhiteHouse Fact Sheets (search via ?s=term)
# -------------------------
def _fetch_whitehouse_fact_sheets_search_html(term: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    base = "https://www.whitehouse.gov/fact-sheets/"
    url = base + "?" + urlencode({"s": term})
    return _fetch_html_requests(url, timeout=timeout)


def _parse_whitehouse_fact_sheets_search_results(html: str, top_n: int = 5) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    _remove_noise(soup)

    items: List[Dict[str, str]] = []
    date_re = re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b"
    )

    for li in soup.select("div.wp-block-query ul.wp-block-post-template > li"):
        a = li.select_one("h2.wp-block-post-title a[href]")
        if not a:
            a = li.select_one("a.wp-block-post-title__link[href]")
        if not a:
            continue

        title = _clean_text(a.get_text(" ", strip=True))
        link = urljoin("https://www.whitehouse.gov/", (a.get("href") or "").strip())
        if not title:
            continue

        date = ""
        t = li.select_one("time")
        if t:
            date = _clean_text(t.get_text(" ", strip=True))

        if not date:
            txt = _clean_text(li.get_text(" ", strip=True))
            m = date_re.search(txt)
            if m:
                date = m.group(0)

        if not date:
            continue

        items.append({"date": date, "announcement": title, "link": link})
        if len(items) >= top_n:
            break

    return items


def _fetch_whitehouse_fact_sheets_top(term: str, top_n: int = 5) -> List[Dict[str, str]]:
    html = _fetch_whitehouse_fact_sheets_search_html(term)
    return _parse_whitehouse_fact_sheets_search_results(html, top_n=top_n)


# -------------------------
# 3) CBP Documents Library (Playwright search)
# -------------------------
def _fetch_cbp_documents_library_search_html(term: str, timeout_ms: int = 60000) -> str:
    if sync_playwright is None:
        raise RuntimeError("Playwright is required for CBP documents-library search.")

    url = "https://www.cbp.gov/documents-library"

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        page.goto(url, wait_until="networkidle", timeout=timeout_ms)

        # Fill "Word or Phrase"
        try:
            page.get_by_label("Word or Phrase").fill(term)
        except Exception:
            page.locator("input[type='text']").first.fill(term)

        # Click Apply
        try:
            page.get_by_role("button", name=re.compile(r"apply", re.IGNORECASE)).click()
        except Exception:
            page.locator("button:has-text('Apply')").click()

        # Wait for results
        page.wait_for_selector("div.view-content div.item-list ol.usa-list--unstyled > li", timeout=timeout_ms)

        html = page.content()
        browser.close()
        return html


def _parse_cbp_documents_library_results(html: str, base_url: str, top_n: int = 5) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    _remove_noise(soup)

    out: List[Dict[str, str]] = []
    rows = soup.select("div.view-content div.item-list ol.usa-list--unstyled > li")

    for li in rows:
        a = li.select_one("a[href]")
        if not a:
            continue

        title = _clean_text(a.get_text(" ", strip=True))
        link = urljoin(base_url, (a.get("href") or "").strip())
        if not title:
            continue

        txt = li.get_text(" ", strip=True)

        # Robust: find Mon Day Year pattern from tokens
        toks = re.findall(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{1,2}|\d{4})\b", txt)
        date = ""
        for i in range(len(toks) - 2):
            if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$", toks[i]) and re.match(r"^\d{1,2}$", toks[i+1]) and re.match(r"^\d{4}$", toks[i+2]):
                date = f"{toks[i]} {toks[i+1]} {toks[i+2]}"
                break

        if not date:
            continue

        out.append({"date": date, "announcement": title, "link": link})
        if len(out) >= top_n:
            break

    return out


def _fetch_cbp_documents_library_top(term: str, top_n: int = 5) -> List[Dict[str, str]]:
    url = "https://www.cbp.gov/documents-library"
    html = _fetch_cbp_documents_library_search_html(term)
    return _parse_cbp_documents_library_results(html, base_url=url, top_n=top_n)


# -------------------------
# 4) Federal Register (API, newest)
# -------------------------
def _fetch_federalregister_top(term: str, top_n: int = 5, timeout: int = DEFAULT_TIMEOUT) -> List[Dict[str, str]]:
    api_url = "https://www.federalregister.gov/api/v1/documents.json"
    params = {
        "per_page": top_n,
        "order": "newest",          # same as clicking "Newest"
        "conditions[term]": term,
    }

    r = _get_session().get(api_url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    items: List[Dict[str, str]] = []
    for d in (data.get("results") or [])[:top_n]:
        title = _clean_text(d.get("title") or "")
        date = _clean_text(d.get("publication_date") or "")
        link = _clean_text(d.get("html_url") or "")
        if title and date:
            items.append({"date": date, "announcement": title, "link": link})
    return items


# -------------------------
# Main scrape functions
# -------------------------
def scrape_one(url: str) -> PageResult:
    # 1) CBP CSMS
    if _is_cbp_csms(url):
        html = _fetch_cbp_csms_html()
        announcements = _parse_cbp_csms_announcements(html)

        # main_text = title — published
        lines = []
        for a in announcements:
            t = (a.get("title") or "").strip()
            d = (a.get("published") or "").strip()
            if t:
                lines.append(f"{t} — {d}".strip(" —"))
        main_text = _fix_mojibake(_clean_text("\n".join(lines)))

        keywords = _basic_keywords(main_text, top_k=12)
        summary, key_points = _extractive_summary(main_text, keywords, max_sentences=6)
        entities = _spacy_entities(main_text) or _regex_entities(main_text)

        extracted_links = [a["link"] for a in announcements if a.get("link")]

        return PageResult(
            url=url,
            title="CBP CSMS Recent Messages",
            main_text=main_text,
            summary=summary,
            key_points=key_points,
            entities=entities,
            keywords=keywords,
            extracted_links=extracted_links,
            announcements=announcements,
        )

    # 2) WhiteHouse Fact Sheets: top 5 for Tariff
    if _is_whitehouse_fact_sheets(url):
        items = _fetch_whitehouse_fact_sheets_top("Tariff", top_n=5)
        main_text = _as_markdown_table_date_announcement(items)

        keywords = _basic_keywords(main_text, top_k=12)
        summary, key_points = _extractive_summary(main_text, keywords, max_sentences=6)
        entities = _spacy_entities(main_text) or _regex_entities(main_text)

        extracted_links = [x["link"] for x in items if x.get("link")]
        announcements = [{"date": x["date"], "title": x["announcement"], "link": x["link"]} for x in items]

        return PageResult(
            url=url,
            title="White House Fact Sheets — Tariff (top 5)",
            main_text=main_text,
            summary=summary,
            key_points=key_points,
            entities=entities,
            keywords=keywords,
            extracted_links=extracted_links,
            announcements=announcements,
        )

    # 3) CBP Documents Library: top 5 for tariff
    if _is_cbp_documents_library(url):
        items = _fetch_cbp_documents_library_top("tariff", top_n=5)
        main_text = _as_markdown_table_date_announcement(items)

        keywords = _basic_keywords(main_text, top_k=12)
        summary, key_points = _extractive_summary(main_text, keywords, max_sentences=6)
        entities = _spacy_entities(main_text) or _regex_entities(main_text)

        extracted_links = [x["link"] for x in items if x.get("link")]
        announcements = [{"date": x["date"], "title": x["announcement"], "link": x["link"]} for x in items]

        return PageResult(
            url=url,
            title="CBP Documents Library — tariff (top 5)",
            main_text=main_text,
            summary=summary,
            key_points=key_points,
            entities=entities,
            keywords=keywords,
            extracted_links=extracted_links,
            announcements=announcements,
        )

    # 4) Federal Register: “Tariff Rates” newest top 5
    if _is_federalregister_search(url):
        items = _fetch_federalregister_top("Tariff Rates", top_n=5)
        main_text = _as_markdown_table_date_announcement(items)

        keywords = _basic_keywords(main_text, top_k=12)
        summary, key_points = _extractive_summary(main_text, keywords, max_sentences=6)
        entities = _spacy_entities(main_text) or _regex_entities(main_text)

        extracted_links = [x["link"] for x in items if x.get("link")]
        announcements = [{"date": x["date"], "title": x["announcement"], "link": x["link"]} for x in items]

        return PageResult(
            url=url,
            title="Federal Register — Tariff Rates (top 5 newest)",
            main_text=main_text,
            summary=summary,
            key_points=key_points,
            entities=entities,
            keywords=keywords,
            extracted_links=extracted_links,
            announcements=announcements,
        )

    # Default: normal page via requests
    html = _fetch_html_requests(url)
    title, main_text, links = _extract_main_text_and_links(html, url)

    if len(main_text) < 300:
        soup = BeautifulSoup(html, "html.parser")
        _remove_noise(soup)
        main_text = _fix_mojibake(_clean_text(soup.get_text(" ", strip=True)))

    keywords = _basic_keywords(main_text, top_k=12)
    summary, key_points = _extractive_summary(main_text, keywords, max_sentences=6)
    entities = _spacy_entities(main_text) or _regex_entities(main_text)

    return PageResult(
        url=url,
        title=title,
        main_text=main_text,
        summary=summary,
        key_points=key_points,
        entities=entities,
        keywords=keywords,
        extracted_links=links,
        announcements=[],
    )


def scrape_all(urls: List[str]) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for u in urls:
        try:
            res = scrape_one(u)
            results.append(asdict(res))
        except Exception as e:
            errors.append({"url": u, "error": str(e)})
        time.sleep(DEFAULT_SLEEP)

    return {"count": len(results), "results": results, "errors": errors}


def _parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Targeted scraper")
    p.add_argument("--urls", nargs="*", default=None, help="Override SOURCES with explicit URL list")
    p.add_argument("--out", default="", help="Write JSON output to this file")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_cli()
    urls = args.urls if args.urls else SOURCES
    out = scrape_all(urls)

    text = json.dumps(out, ensure_ascii=False, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
