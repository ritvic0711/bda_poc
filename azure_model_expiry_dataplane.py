#!/usr/bin/env python3
"""
Azure OpenAI model expiration WITHOUT management/ARM credentials.

You have: endpoint URL + API key (+ model names).
Strategy:
  1. Data-plane call lists your deployments and their model_version.
  2. Scrape Microsoft's LIVE model-retirements page (always current).
  3. Join your deployed (model, version) -> retirement date.

The retirement dates are NOT available from the data-plane API, so the
official doc page is the source of truth. We fetch it live rather than
hardcoding, because Microsoft revises it frequently.

pip install requests beautifulsoup4 lxml
"""

import sys
import csv
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---- Config -------------------------------------------------------------
ENDPOINT = "https://YOUR-RESOURCE.openai.azure.com"   # your model URL root
API_KEY = "YOUR-SUBSCRIPTION-KEY"
API_VERSION = "2024-10-21"
RETIREMENT_URL = (
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/"
    "concepts/model-retirements"
)
# Fallback if the URL above 404s (MS moves it occasionally):
RETIREMENT_URL_ALT = (
    "https://learn.microsoft.com/en-us/azure/ai-services/openai/"
    "concepts/model-retirements"
)
# -------------------------------------------------------------------------


def list_deployments():
    """Data-plane: your actual deployments + model versions."""
    url = f"{ENDPOINT}/openai/deployments"
    r = requests.get(url, headers={"api-key": API_KEY},
                     params={"api-version": API_VERSION}, timeout=30)
    r.raise_for_status()
    out = []
    for d in r.json().get("data", []):
        out.append({
            "deployment": d.get("id", ""),
            "model": (d.get("model") or "").lower(),
            "version": str(d.get("model_version") or ""),
        })
    return out


def _parse_date(text):
    """Pull a date like 'April 02, 2025' or '2025-04-02' out of a cell."""
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        m = re.search(r"[A-Za-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2}", text)
        if m:
            try:
                return datetime.strptime(m.group(0), fmt).date()
            except ValueError:
                continue
    return None


def scrape_retirement_table():
    """
    Scrape the live retirement table into:
      { (model_lower, version): retirement_date_str }
    Table columns vary (sometimes Legacy/Deprecation/Retirement), so we
    grab the model, version, and the LAST date-looking cell as retirement.
    """
    html = None
    for url in (RETIREMENT_URL, RETIREMENT_URL_ALT):
        try:
            resp = requests.get(url, timeout=30,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.ok:
                html = resp.text
                break
        except requests.RequestException:
            continue
    if not html:
        print("    ! could not fetch retirement page", file=sys.stderr)
        return {}

    soup = BeautifulSoup(html, "lxml")
    lookup = {}
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower()
                   for th in table.find_all("th")]
        if not any("model" in h for h in headers):
            continue
        # identify column indices
        try:
            mi = next(i for i, h in enumerate(headers) if "model" in h)
        except StopIteration:
            continue
        vi = next((i for i, h in enumerate(headers) if "version" in h), None)

        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) <= mi:
                continue
            model = cells[mi].lower().strip()
            if not model:
                continue
            version = cells[vi].strip() if vi is not None and len(cells) > vi else ""
            # retirement = last cell that parses as a date
            retire = ""
            for c in reversed(cells):
                d = _parse_date(c)
                if d:
                    retire = d.isoformat()
                    break
            lookup[(model, version)] = retire
            # also index without version, as a fallback
            lookup.setdefault((model, ""), retire)
    return lookup


def days_left(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return (d - datetime.now(timezone.utc)).days


def main():
    print("[*] Listing deployments from your endpoint...")
    deployments = list_deployments()
    if not deployments:
        sys.exit("No deployments returned. Check endpoint/key.")

    print("[*] Scraping live Microsoft retirement table...")
    table = scrape_retirement_table()

    rows = []
    for dep in deployments:
        key = (dep["model"], dep["version"])
        retire = table.get(key) or table.get((dep["model"], "")) or ""
        rows.append({
            **dep,
            "retirement_date": retire or "not found",
            "days_left": days_left(retire),
        })

    rows.sort(key=lambda r: (r["days_left"] == "", r["days_left"]))

    print(f"\n{'DEPLOYMENT':<28}{'MODEL':<22}{'VER':<12}"
          f"{'RETIRES':<14}{'DAYS':<6}")
    print("-" * 82)
    for r in rows:
        print(f"{r['deployment']:<28}{r['model']:<22}{r['version']:<12}"
              f"{r['retirement_date']:<14}{r['days_left']}")

    with open("azure_model_expiry.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print("\n[+] Wrote azure_model_expiry.csv")


if __name__ == "__main__":
    main()
