#!/usr/bin/env python3
"""
model_lifecycle.py — one dispatcher for model lifecycle/expiration enrichment
across providers (Azure OpenAI, AWS Bedrock).

Both providers feed the SAME dataframe and write the SAME shared columns, so
downstream code never has to care which cloud a row came from.

Public API
----------
    enrich(df, provider, **kwargs) -> new dataframe with shared columns

    provider = "azure"    -> scrapes the public MS retirement table (no creds)
    provider = "bedrock"  -> delegates to bedrock_enrich.enrich (boto3 creds)

Shared columns (ALWAYS present after enrich):
    lifecycle_status        ACTIVE / LEGACY / EOL / UNKNOWN
    deprecation_date        ISO date the model stops taking NEW deployments ("" if none)
    eol_date                ISO date the model stops working entirely
                            (Azure "retirement" == Bedrock "end-of-life") ("" if none)
    days_until_eol          int days from today to eol_date ("" if unknown)

Provider-specific columns are appended only for that provider's rows:
    bedrock -> inference_type, allocated_rpm, allocated_tpm

Install:
    pip install pandas aiohttp beautifulsoup4 lxml      # azure path
    pip install boto3                                   # bedrock path
"""

import re
import sys
import asyncio
from datetime import datetime, timezone

import pandas as pd

# ---- Shared schema ------------------------------------------------------
SHARED_COLS = ("lifecycle_status", "deprecation_date", "eol_date", "days_until_eol")
# -------------------------------------------------------------------------


# =========================================================================
# Shared helpers
# =========================================================================
_DATE_RE = re.compile(r"[A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2}")


def parse_date(text):
    """Any supported date string -> datetime.date, or None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    s = m.group(0).replace(",", "")
    for fmt in ("%B %d %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def days_until(iso):
    """ISO date string -> whole days from now (UTC), or "" if unparseable/empty."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return (d - datetime.now(timezone.utc)).days


def _derive_status(deprecation_iso, eol_iso):
    """Derive a lifecycle status when the provider doesn't give one (Azure)."""
    today = datetime.now(timezone.utc).date()
    try:
        eol = datetime.fromisoformat(eol_iso).date() if eol_iso else None
    except ValueError:
        eol = None
    try:
        dep = datetime.fromisoformat(deprecation_iso).date() if deprecation_iso else None
    except ValueError:
        dep = None
    if eol and eol <= today:
        return "EOL"
    if dep and dep <= today:
        return "LEGACY"
    if eol or dep:
        return "ACTIVE"
    return "UNKNOWN"


def _finalize(df):
    """Ensure shared cols exist, in order, and days_until_eol is coherent."""
    for c in SHARED_COLS:
        if c not in df.columns:
            df[c] = ""
    df["days_until_eol"] = df["eol_date"].apply(days_until)
    return df


# =========================================================================
# Azure path (public scrape, async fetch, no credentials)
# =========================================================================
_AZURE_URLS = [
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements",
    "https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/model-retirements",
]
_AZURE_HEADERS = {"User-Agent": "Mozilla/5.0"}


async def _azure_fetch_first_good():
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def fetch(url):
            try:
                async with session.get(url, headers=_AZURE_HEADERS) as resp:
                    if resp.status == 200:
                        txt = await resp.text()
                        if "retirement" in txt.lower():
                            return url, txt
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"    ! {url} failed: {e}", file=sys.stderr)
            return url, None

        tasks = [asyncio.create_task(fetch(u)) for u in _AZURE_URLS]
        for coro in asyncio.as_completed(tasks):
            url, html = await coro
            if html:
                print(f"[*] Fetched retirement table: {url}")
                for t in tasks:
                    t.cancel()
                return html
    sys.exit("Could not fetch any Azure retirement page.")


def _azure_scrape(html):
    """{model_lower: {'deprecation': iso, 'eol': iso}} from the MS table."""
    from bs4 import BeautifulSoup
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
            if ret is None:  # fallback: last date-looking cell is the retirement/eol date
                for c in reversed(cells):
                    ret = parse_date(c)
                    if ret:
                        break
            lookup[model] = {
                "deprecation": dep.isoformat() if dep else "",
                "eol": ret.isoformat() if ret else "",
            }
    return lookup


def _azure_match(lookup, model_id):
    if not isinstance(model_id, str) or not model_id.strip():
        return {"deprecation": "", "eol": ""}
    m = model_id.strip().lower()
    if m in lookup:
        return lookup[m]
    norm = m.replace("gpt-3.5", "gpt-35").replace("gpt-4.1", "gpt-41")
    if norm in lookup:
        return lookup[norm]
    for k, v in lookup.items():
        if m in k or k in m:
            return v
    return {"deprecation": "", "eol": ""}


def _run_coro(coro):
    """
    Run an async coroutine to completion from EITHER a sync context or one
    that already has a running event loop.

    asyncio.run() raises "cannot be called from a running event loop" when the
    caller (e.g. an async test suite) is already inside a loop. In that case we
    run the coroutine on a dedicated thread with its own loop and block for the
    result. When there's no running loop, we just use asyncio.run().
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)          # no loop running -> normal path

    # A loop is already running in this thread -> offload to a worker thread.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _enrich_azure(df, model_id_col="model_id", **_ignored):
    if model_id_col not in df.columns:
        raise KeyError(f"'{model_id_col}' not in columns: {list(df.columns)}")
    out = df.copy()
    html = _run_coro(_azure_fetch_first_good())
    lookup = _azure_scrape(html)
    print(f"[*] Parsed {len(lookup)} Azure models from the table.")

    hits = out[model_id_col].apply(lambda x: _azure_match(lookup, x))
    out["deprecation_date"] = hits.apply(lambda h: h["deprecation"])
    out["eol_date"] = hits.apply(lambda h: h["eol"])
    out["lifecycle_status"] = out.apply(
        lambda r: _derive_status(r["deprecation_date"], r["eol_date"]), axis=1)
    return out


# =========================================================================
# Bedrock path (delegates to your existing bedrock_enrich.py)
# =========================================================================
def _enrich_bedrock(df, **kwargs):
    """
    Delegate to bedrock_enrich.enrich, then normalize its columns into the
    shared schema. bedrock_enrich already emits:
        lifecycle_status, is_legacy, eol_date, inference_type,
        allocated_rpm, allocated_tpm
    We map: eol_date stays; add deprecation_date ("" — Bedrock has no explicit
    deprecation date, only the LEGACY flag); keep provider-specific cols.
    """
    try:
        import bedrock_enrich
    except ImportError:
        sys.exit("bedrock_enrich.py must be importable for provider='bedrock'.")

    out = bedrock_enrich.enrich(df, **kwargs)

    # Bedrock has no separate deprecation DATE; LEGACY is a status, not a date.
    if "deprecation_date" not in out.columns:
        out["deprecation_date"] = ""
    # Drop columns we never want in the final sheet:
    #   is_legacy    — redundant with lifecycle_status == "LEGACY"
    #   enrich_region, enrich_note — internal diagnostics from older bedrock_enrich
    #                                versions; not part of the shared schema.
    drop = [c for c in ("is_legacy", "enrich_region", "enrich_note") if c in out.columns]
    if drop:
        out = out.drop(columns=drop)
    return out


# =========================================================================
# Dispatcher
# =========================================================================
_PROVIDERS = {
    "azure": _enrich_azure,
    "bedrock": _enrich_bedrock,
}

# Default mapping of raw provider labels -> canonical keys. Case-insensitive.
# Override/extend via the `provider_map` argument when your labels differ.
_DEFAULT_PROVIDER_MAP = {
    "azure": "azure",
    "azure openai": "azure",
    "azureopenai": "azure",
    "openai": "azure",
    "bedrock": "bedrock",
    "aws": "bedrock",
    "aws bedrock": "bedrock",
    "amazon": "bedrock",
    "amazon bedrock": "bedrock",
}


def _canon_provider(raw, provider_map):
    """Map one raw cell value to 'azure'/'bedrock', or None if unrecognized."""
    if not isinstance(raw, str):
        return None
    return provider_map.get(raw.strip().lower())


def enrich(df, provider=None, provider_col=None, provider_map=None,
           on_unknown_provider="error", **kwargs):
    """
    Enrich `df` with shared lifecycle columns.

    Two modes:

    1) Single-provider frame — pass `provider="azure"` or `"bedrock"`.
       Every row is treated as that provider.

    2) Mixed frame — pass `provider_col="<column name>"` (e.g. "Vendor").
       The frame is split by that column, each group is enriched by its own
       provider path, and the result is recombined in the ORIGINAL row order.
       Shared columns are identical across groups; provider-specific columns
       (inference_type, allocated_rpm/tpm) are filled only on the rows whose
       provider produces them, blank elsewhere.

    Parameters
    ----------
    provider      : "azure" | "bedrock"  (single-provider mode). Ignored if
                    provider_col is given.
    provider_col  : column holding a per-row provider label (mixed mode).
    provider_map  : dict of {raw_label_lowercased: "azure"|"bedrock"} to
                    translate your labels. Merged over the built-in defaults,
                    so you only supply the ones that differ.
    on_unknown_provider : "error" (raise) | "skip" (leave those rows'
                    shared cols blank) — for labels that don't map.
    kwargs        : passed through to each provider enricher. Common ones:
                      azure   -> model_id_col="model_id"
                      bedrock -> model_id_col, model_name_col, account_col,
                                 account_to_profile, default_region, verbose...
                    Provider enrichers ignore kwargs they don't use, so one
                    call with a superset of kwargs is fine for a mixed frame.
    """
    pmap = {**_DEFAULT_PROVIDER_MAP, **{k.lower(): v for k, v in (provider_map or {}).items()}}

    # -------- single-provider mode --------
    if provider_col is None:
        p = (provider or "").lower()
        if p not in _PROVIDERS:
            raise ValueError(
                f"provider must be one of {list(_PROVIDERS)} (or pass provider_col "
                f"for a mixed frame), got {provider!r}")
        return _finalize(_PROVIDERS[p](df, **kwargs))

    # -------- mixed mode: split -> enrich per group -> recombine --------
    if provider_col not in df.columns:
        raise KeyError(f"provider_col '{provider_col}' not in columns: {list(df.columns)}")

    work = df.copy()
    work["_orig_order"] = range(len(work))
    work["_canon"] = work[provider_col].apply(lambda v: _canon_provider(v, pmap))

    unknown = work[work["_canon"].isna()]
    if len(unknown):
        labels = sorted(set(unknown[provider_col].dropna().astype(str)))
        if on_unknown_provider == "error":
            raise ValueError(
                f"Unrecognized provider label(s) in '{provider_col}': {labels}. "
                f"Add them to provider_map, or pass on_unknown_provider='skip'.")
        print(f"    ! skipping {len(unknown)} row(s) with unknown provider: {labels}",
              file=sys.stderr)

    parts = []
    for canon, group in work.groupby("_canon", sort=False):
        enriched = _finalize(_PROVIDERS[canon](group.drop(columns=["_canon", "_orig_order"]),
                                               **kwargs))
        enriched["_orig_order"] = group["_orig_order"].values
        parts.append(enriched)

    if len(unknown):                       # carry skipped rows through, blank shared cols
        skipped = _finalize(unknown.drop(columns=["_canon", "_orig_order"]).copy())
        skipped["_orig_order"] = unknown["_orig_order"].values
        parts.append(skipped)

    result = (pd.concat(parts, ignore_index=True)
                .sort_values("_orig_order")
                .drop(columns="_orig_order")
                .reset_index(drop=True))
    return result
