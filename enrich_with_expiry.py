#!/usr/bin/env python3
"""
Enrich an aggregated model report CSV with Azure OpenAI retirement dates.

- Reads your CSV into a DataFrame.
- Scrapes Microsoft's PUBLIC model-retirements page (no credentials).
- Matches each row's model_id -> deprecation / retirement dates.
- Adds columns: deprecation_date, retirement_date, days_until_retirement.
- Writes a new CSV (does not overwrite the original).

pip install pandas requests beautifulsoup4 lxml
"""

import re
import sys
from datetime import datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---- Config -------------------------------------------------------------
INPUT_CSV = "enriched_aggregated_report.csv"          # your file
OUTPUT_CSV = "enriched_aggregated_report_with_expiry.csv"
MATCH_COLUMN = "model_id"   # column holding gpt-41, gpt-4o-mini, o3-mini...
# -------------------------------------------------------------------------

URLS = [
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements",
    "https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/model-retirements",
]
HEADERS = {"User-Agent": "Mozilla/5.0"}
DATE_RE = re.compile(r"[A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2}")


def fetch_html():
    for url in URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.ok and "retirement" in r.text.lower():
                print(f"[*] Fetched retirement table: {url}")
                return r.text
        except requests.RequestException:
            continue
    sys.exit("Could not fetch any retirement page.")


def parse_date(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    s = m.group(0).replace(",", "")
    for fmt in ("%B %d %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def scrape(html):
    """Return {model_lower: {'deprecation': iso, 'retirement': iso}}."""
    soup = BeautifulSoup(html, "lxml")
    lookup = {}
    for table in soup.find_all("table"):
        heads = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not any("model" in h for h in heads):
            continue
        mi = next((i for i, h in enumerate(heads) if "model" in h), None)
        di = next((i for i, h in enumerate(heads) if "deprecat" in h), None)
        ri = next((i for i, h in enumerate(heads) if "retire" in h), None)
        if mi is None:
            continue
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) <= mi or not cells[mi].strip():
                continue
            model = cells[mi].strip().lower()
            dep = parse_date(cells[di]) if di is not None and len(cells) > di else None
            ret = parse_date(cells[ri]) if ri is not None and len(cells) > ri else None
            if ret is None:  # fallback: last date-looking cell
                for c in reversed(cells):
                    ret = parse_date(c)
                    if ret:
                        break
            lookup[model] = {
                "deprecation": dep.isoformat() if dep else "",
                "retirement": ret.isoformat() if ret else "",
            }
    return lookup


def match(lookup, model_id):
    """Exact, then normalized, then substring match."""
    if not isinstance(model_id, str) or not model_id.strip():
        return {"deprecation": "", "retirement": ""}
    m = model_id.strip().lower()
    if m in lookup:
        return lookup[m]
    # normalize gpt-35 <-> gpt-3.5 style differences
    norm = m.replace("gpt-3.5", "gpt-35").replace("gpt-4.1", "gpt-41")
    if norm in lookup:
        return lookup[norm]
    for k, v in lookup.items():
        if m in k or k in m:
            return v
    return {"deprecation": "", "retirement": ""}


def days_until(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return (d - datetime.now(timezone.utc)).days


def main():
    df = pd.read_csv(INPUT_CSV)
    if MATCH_COLUMN not in df.columns:
        sys.exit(f"Column '{MATCH_COLUMN}' not in CSV. Found: {list(df.columns)}")

    lookup = scrape(fetch_html())
    print(f"[*] Parsed {len(lookup)} models from the table.")

    hits = df[MATCH_COLUMN].apply(lambda x: match(lookup, x))
    df["deprecation_date"] = hits.apply(lambda h: h["deprecation"])
    df["retirement_date"] = hits.apply(lambda h: h["retirement"])
    df["days_until_retirement"] = df["retirement_date"].apply(days_until)

    df.to_csv(OUTPUT_CSV, index=False)
    found = (df["retirement_date"] != "").sum()
    print(f"[+] Matched retirement dates for {found}/{len(df)} rows.")
    print(f"[+] Wrote {OUTPUT_CSV}")

    # quick console peek, soonest-expiring first
    peek = df[[MATCH_COLUMN, "retirement_date", "days_until_retirement"]].copy()
    peek["_sort"] = pd.to_numeric(peek["days_until_retirement"], errors="coerce")
    print("\n" + peek.sort_values("_sort").drop(columns="_sort")
          .to_string(index=False))


if __name__ == "__main__":
    main()
