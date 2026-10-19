#!/usr/bin/env python3
"""
Azure OpenAI model expiration with NO credentials at all.

You supply model names (and optionally versions). The script scrapes
Microsoft's PUBLIC model-retirements doc page and reports the
deprecation / retirement dates for each one.

pip install requests beautifulsoup4 lxml
"""

import csv
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---- Your models. Version optional ("" = any/latest listed). ----
MODELS = [
    ("gpt-4o", "2024-11-20"),
    ("gpt-4o-mini", ""),
    ("gpt-35-turbo", ""),
    ("text-embedding-3-large", ""),
    # ("your-model", "your-version"),
]

# Public docs — no auth. Foundry page first, legacy path as fallback.
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
                print(f"[*] Fetched: {url}")
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
    """
    Returns list of dicts: {model, version, deprecation, retirement}
    Column layouts vary, so we locate columns by header name and treat
    the last date cell as retirement when headers are ambiguous.
    """
    soup = BeautifulSoup(html, "lxml")
    records = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not any("model" in h for h in headers):
            continue
        idx = {name: i for i, name in enumerate(headers)}
        mi = next((i for h, i in idx.items() if "model" in h), None)
        vi = next((i for h, i in idx.items() if "version" in h), None)
        di = next((i for h, i in idx.items() if "deprecat" in h), None)
        ri = next((i for h, i in idx.items() if "retire" in h), None)
        if mi is None:
            continue

        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) <= mi or not cells[mi].strip():
                continue
            model = cells[mi].strip()
            version = cells[vi].strip() if vi is not None and len(cells) > vi else ""

            dep = parse_date(cells[di]) if di is not None and len(cells) > di else None
            ret = parse_date(cells[ri]) if ri is not None and len(cells) > ri else None
            if ret is None:  # fallback: last date-looking cell
                for c in reversed(cells):
                    ret = parse_date(c)
                    if ret:
                        break
            records.append({
                "model": model,
                "version": version,
                "deprecation": dep.isoformat() if dep else "",
                "retirement": ret.isoformat() if ret else "",
            })
    return records


def days_left(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return (d - datetime.now(timezone.utc)).days


def match(records, model, version):
    model_l = model.lower()
    # exact model+version, then model-only, then substring on model
    for r in records:
        if r["model"].lower() == model_l and (not version or r["version"] == version):
            return r
    for r in records:
        if r["model"].lower() == model_l:
            return r
    for r in records:
        if model_l in r["model"].lower():
            return r
    return None


def main():
    records = scrape(fetch_html())
    print(f"[*] Parsed {len(records)} rows from the table(s).\n")

    out = []
    for model, version in MODELS:
        hit = match(records, model, version)
        if hit:
            out.append({
                "requested_model": model,
                "requested_version": version,
                "matched_model": hit["model"],
                "matched_version": hit["version"],
                "deprecation": hit["deprecation"],
                "retirement": hit["retirement"],
                "days_left": days_left(hit["retirement"]),
            })
        else:
            out.append({
                "requested_model": model, "requested_version": version,
                "matched_model": "NOT FOUND", "matched_version": "",
                "deprecation": "", "retirement": "", "days_left": "",
            })

    out.sort(key=lambda r: (r["days_left"] == "", r["days_left"]))
    print(f"{'MODEL':<26}{'VER':<14}{'DEPRECATED':<14}{'RETIRES':<14}{'DAYS':<6}")
    print("-" * 74)
    for r in out:
        print(f"{r['requested_model']:<26}{r['requested_version']:<14}"
              f"{r['deprecation'] or '-':<14}{r['retirement'] or '-':<14}"
              f"{r['days_left']}")

    with open("azure_model_expiry.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out[0].keys())
        w.writeheader()
        w.writerows(out)
    print("\n[+] Wrote azure_model_expiry.csv")


if __name__ == "__main__":
    main()
