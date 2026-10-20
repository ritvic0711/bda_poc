#!/usr/bin/env python3
"""
bedrock_enrich.py

Enrich an existing Bedrock model dataframe with lifecycle status, EOL date, and
per-model TPM/RPM quotas + utilisation.

You already have a dataframe (like the aggregated_report sheet) with columns such as
"Model Name", "Model ID", "Environment", "Account", "Vendor", "Avg Latency", ...
This module ADDS columns to it; it does not query "which models do I use" — your df
already is that list.

Public API (async):
    await enrich(df, ...) -> df     # coroutine; returns a NEW dataframe with enrichment cols
    enrich_sync(df, ...) -> df      # convenience wrapper that runs the coroutine for you

boto3 has no native async, so blocking AWS/urllib calls are offloaded to threads via
asyncio.to_thread and the rows are processed concurrently under a bounded semaphore
(max_concurrency). The one-off ListFoundationModels / Health calls run once up front.

Columns added:
    lifecycle_status   ACTIVE / LEGACY / EOL / UNKNOWN   (ListFoundationModels, else "")
    is_legacy          True/False
    eol_date           for LEGACY/EOL models (Health API -> model-card scrape); else ""
    applied_rpm        your account's applied requests-per-minute quota (Service Quotas)
    applied_tpm        your account's applied tokens-per-minute quota
    invocations        sum over lookback window (CloudWatch)        [utilisation]
    input_tokens       sum over window
    output_tokens      sum over window
    throttles          sum over window
    peak_est_tpm_use   peak per-minute EstimatedTPMQuotaUsage
    tpm_pct_of_quota   peak_est_tpm_use / applied_tpm * 100
    enrich_region      region actually queried for this row
    enrich_note        per-row diagnostics (why a cell is blank, region assumptions, etc.)

Data sources, per the two-source design:
  - Lifecycle + EOL: live ListFoundationModels (authoritative status), with the Bedrock
    model-card page scrape as the EOL-date fallback (the API returns status, not the date).
    These need only bedrock:ListFoundationModels (any one working credential), so they run
    once globally, not per account.
  - TPM/RPM + utilisation: live per row, using the row's Account -> AWS profile mapping and
    the region inferred from the Model ID prefix. Rows with no mapped profile get lifecycle
    /EOL but blank quotas/utilisation (noted in enrich_note), rather than failing.

Region inference from Model ID prefix:
    us.*  / eu.* / apac.*  -> that geo's default region (overridable via geo_default_region)
    global.*               -> NOT parseable (global routing); uses default_region, and the
                              utilisation/quota figures then reflect only that one region.
    bare id (no prefix)    -> default_region

Requires: boto3, pandas. AWS creds via the profiles you map (or the default chain).
IAM: bedrock:ListFoundationModels, cloudwatch:GetMetricStatistics,
     servicequotas:ListServiceQuotas  (+ health:DescribeEvents* optional for EOL).
"""

import asyncio
import datetime as dt
import re
import sys
import urllib.request

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
except ImportError:
    sys.exit("boto3 not installed. Run: pip install boto3")

try:
    import pandas as pd
except ImportError:
    sys.exit("pandas not installed. Run: pip install pandas")

NS = "AWS/Bedrock"
DOCS_BASE = "https://docs.aws.amazon.com/bedrock/latest/userguide"
HEALTH_REGION = "us-east-1"

# Geo-prefix -> a sensible default region to query for that geo. Override via geo_default_region.
GEO_DEFAULT_REGION = {
    "us": "us-east-1",
    "eu": "eu-west-1",
    "apac": "ap-southeast-1",
    "ap": "ap-southeast-1",
}
GEO_PREFIXES = set(GEO_DEFAULT_REGION) | {"global", "ca", "sa"}

_DATE_PATTERNS = [
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
    r"\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
    r"\d{4}-\d{2}-\d{2}",
]


# ---------------------------------------------------------------------------
# Model-ID parsing
# ---------------------------------------------------------------------------
def strip_geo_prefix(model_id):
    """
    Return (base_id, geo_prefix_or_None).
    'global.anthropic.claude-fable-5' -> ('anthropic.claude-fable-5', 'global')
    'us.anthropic.claude-haiku-4-5-20251001-v1:0' -> ('anthropic.claude-haiku-...', 'us')
    'anthropic.claude-sonnet-5' -> ('anthropic.claude-sonnet-5', None)
    """
    if not isinstance(model_id, str) or not model_id:
        return "", None
    base = model_id.strip().split("/")[-1]  # drop ARN prefix if any
    parts = base.split(".")
    if len(parts) > 2 and parts[0] in GEO_PREFIXES:
        return ".".join(parts[1:]), parts[0]
    return base, None


def region_for_row(model_id, default_region, geo_default_region):
    """Infer which region to query for this model's quotas/utilisation."""
    _, geo = strip_geo_prefix(model_id)
    if geo in (None, "global"):
        # bare id or global routing profile -> we can't pin a region; use the default
        return default_region
    return geo_default_region.get(geo, default_region)


# ---------------------------------------------------------------------------
# Lifecycle (global, cached) -- ListFoundationModels
# ---------------------------------------------------------------------------
def build_lifecycle_index(session_or_client, verbose=False):
    """
    Call ListFoundationModels once and return a list of
    {modelId, modelName, provider, status}. Used to match each df row's Model ID.
    """
    try:
        bedrock = session_or_client.client("bedrock") \
            if hasattr(session_or_client, "client") else session_or_client
        resp = bedrock.list_foundation_models()
    except (ClientError, NoCredentialsError) as e:
        if verbose:
            print(f"  ! ListFoundationModels failed: {e}", file=sys.stderr)
        return []
    out = []
    for fm in resp.get("modelSummaries", []):
        lc = fm.get("modelLifecycle", {}) or {}
        out.append({
            "modelId": fm.get("modelId", ""),
            "modelName": fm.get("modelName", ""),
            "provider": fm.get("providerName", ""),
            "status": lc.get("status", "UNKNOWN"),
        })
    return out


def match_lifecycle(model_id, fm_list):
    """Match a df Model ID (geo-stripped) against the FM list. Returns the fm dict or None."""
    base, _ = strip_geo_prefix(model_id)
    if not base:
        return None
    base_nover = base.split(":")[0]
    for fm in fm_list:                         # exact / version-prefix
        if fm["modelId"] == base or fm["modelId"].startswith(base_nover):
            return fm
    for fm in fm_list:                         # loose containment
        if base_nover and base_nover in fm["modelId"]:
            return fm
    return None


# ---------------------------------------------------------------------------
# EOL date (global, cached) -- Health API primary, model-card scrape fallback
# ---------------------------------------------------------------------------
def _extract_date(text):
    if not text:
        return None
    for pat in _DATE_PATTERNS:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return None


def build_health_eol_map(session, verbose=False):
    """{model-id-token: eol_date} from AWS Health Bedrock notices. {} if no support plan."""
    out = {}
    try:
        health = session.client("health", region_name=HEALTH_REGION)
        paginator = health.get_paginator("describe_events")
        arns = []
        for page in paginator.paginate(filter={
            "services": ["BEDROCK"],
            "eventTypeCategories": ["scheduledChange", "accountNotification"],
        }):
            arns += [ev["arn"] for ev in page.get("events", [])]
        for i in range(0, len(arns), 10):
            details = health.describe_event_details(eventArns=arns[i:i + 10])
            for d in details.get("successfulSet", []):
                desc = d.get("eventDescription", {}).get("latestDescription", "") or ""
                eol = _extract_date(desc)
                if not eol:
                    continue
                for tok in re.findall(r"[a-z0-9]+\.[a-z0-9][a-z0-9\-.:]+", desc.lower()):
                    out[tok] = eol
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "SubscriptionRequiredException":
            if verbose:
                print("  i Health API needs Business/Enterprise support — using card scrape.",
                      file=sys.stderr)
        elif verbose:
            print(f"  ! Health error: {e}", file=sys.stderr)
    except Exception as e:
        if verbose:
            print(f"  ! Health error: {e}", file=sys.stderr)
    return out


def _card_urls(provider, model_name):
    def slug(s):
        s = (s or "").lower().strip().replace("+", "-plus").replace(".", "-").replace(" ", "-")
        s = re.sub(r"[^a-z0-9\-]", "", s)
        return re.sub(r"-+", "-", s).strip("-")
    prov, name = slug(provider), slug(model_name)
    if not prov or not name:
        return []
    stem = f"model-card-{prov}-{name}"
    return [f"{DOCS_BASE}/{stem}.md", f"{DOCS_BASE}/{stem}.html"]


def eol_from_card(provider, model_name, cache, verbose=False):
    key = (provider, model_name)
    if key in cache:
        return cache[key]
    result = None
    for url in _card_urls(provider, model_name):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bedrock-enrich/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        for line in body.splitlines():
            low = line.lower()
            if "eol" in low or "end-of-life" in low or "end of life" in low:
                d = _extract_date(line)
                if d:
                    result = d
                    break
        if result:
            break
    cache[key] = result
    return result


def resolve_eol(model_id, provider, model_name, health_map, card_cache, verbose=False):
    base, _ = strip_geo_prefix(model_id)
    base = base.lower()
    for k in (base, base.split(":")[0]):
        if k in health_map:
            return health_map[k]
    for hk, hv in health_map.items():
        if hk and (hk in base or base in hk):
            return hv
    return eol_from_card(provider, model_name, card_cache, verbose=verbose)


# ---------------------------------------------------------------------------
# Quotas + utilisation (per region, cached by region)
# ---------------------------------------------------------------------------
def get_applied_quotas(sq, verbose=False):
    quotas = []
    try:
        for page in sq.get_paginator("list_service_quotas").paginate(ServiceCode="bedrock"):
            for q in page["Quotas"]:
                quotas.append((q["QuotaName"], q.get("Value")))
    except ClientError as e:
        if verbose:
            print(f"  ! Service Quotas failed: {e}", file=sys.stderr)
    return quotas


def find_quota(model_name, quotas, kind):
    """kind = 'requests' | 'tokens'. Fuzzy-match model display name against quota names."""
    if not model_name:
        return None
    key = "requests per minute" if kind == "requests" else "tokens per minute"
    toks = [t.lower() for t in str(model_name).replace("-", " ").split() if len(t) > 2]
    best = None
    for qn, qv in quotas:
        ql = qn.lower()
        if key not in ql:
            continue
        if "on-demand" not in ql and "cross-region" not in ql:
            continue
        hits = sum(1 for t in toks if t in ql)
        if toks and hits >= max(1, len(toks) - 1) and (best is None or (qv or 0) > (best or 0)):
            best = qv
    return best


def metric_sum(cw, metric, model_id_for_cw, start, end, stat="Sum"):
    try:
        resp = cw.get_metric_statistics(
            Namespace=NS, MetricName=metric,
            Dimensions=[{"Name": "ModelId", "Value": model_id_for_cw}],
            StartTime=start, EndTime=end, Period=86400, Statistics=[stat])
    except ClientError:
        return None
    pts = resp.get("Datapoints", [])
    if not pts:
        return 0
    return sum(p["Sum"] for p in pts) if stat == "Sum" else max(p["Maximum"] for p in pts)


def est_tpm_peak(cw, model_id_for_cw, start, end):
    try:
        resp = cw.get_metric_statistics(
            Namespace=NS, MetricName="EstimatedTPMQuotaUsage",
            Dimensions=[{"Name": "ModelId", "Value": model_id_for_cw}],
            StartTime=start, EndTime=end, Period=60, Statistics=["Maximum"])
    except ClientError:
        return None
    pts = resp.get("Datapoints", [])
    return max(p["Maximum"] for p in pts) if pts else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
ENRICH_COLS = (
    "lifecycle_status", "is_legacy", "eol_date", "applied_rpm", "applied_tpm",
    "invocations", "input_tokens", "output_tokens", "throttles",
    "peak_est_tpm_use", "tpm_pct_of_quota", "enrich_region", "enrich_note")


async def enrich(df,
                 model_id_col="Model ID",
                 model_name_col="Model Name",
                 account_col="Account",
                 account_to_profile=None,
                 default_region="us-east-1",
                 geo_default_region=None,
                 days=30,
                 resolve_eol_dates=True,
                 lifecycle_session=None,
                 max_concurrency=8,
                 verbose=False):
    """
    Coroutine. Return a NEW dataframe = df + enrichment columns (see module docstring).
    `await enrich(df, ...)`, or call the enrich_sync(...) wrapper from plain sync code.

    boto3 is synchronous, so each blocking AWS/urllib call is offloaded to a worker thread
    with asyncio.to_thread, and the rows run concurrently (bounded by max_concurrency) so the
    per-row CloudWatch/Service-Quotas/card-scrape I/O overlaps instead of running serially.

    Parameters
    ----------
    df : pandas.DataFrame            your aggregated report.
    model_id_col / model_name_col / account_col : column names in df.
    account_to_profile : dict        {account_alias: aws_profile_name}. Rows whose account
                                     isn't mapped get lifecycle/EOL but blank quotas/util.
                                     If None, the default credential chain is used for ALL
                                     rows (fine when every row is one account you're authed to).
    default_region : str             region used for bare ids and global.* models.
    geo_default_region : dict        override GEO_DEFAULT_REGION, e.g. {"eu": "eu-central-1"}.
    days : int                       utilisation lookback window.
    resolve_eol_dates : bool         set False to skip the EOL lookup entirely.
    lifecycle_session : boto3.Session creds for the one global ListFoundationModels / Health
                                     call. Defaults to the first mapped profile, else default chain.
    max_concurrency : int            max rows processed at once (caps simultaneous AWS calls).
    """
    if model_id_col not in df.columns:
        raise KeyError(f"'{model_id_col}' not in dataframe columns: {list(df.columns)}")

    out = df.copy()
    account_to_profile = account_to_profile or {}
    geo_defaults = {**GEO_DEFAULT_REGION, **(geo_default_region or {})}

    # --- one global session for lifecycle/EOL (creds-light), built off-thread ---
    if lifecycle_session is None:
        if account_to_profile:
            first_profile = next(iter(account_to_profile.values()))
            try:
                lifecycle_session = boto3.Session(profile_name=first_profile,
                                                  region_name=default_region)
            except ProfileNotFound:
                lifecycle_session = boto3.Session(region_name=default_region)
        else:
            lifecycle_session = boto3.Session(region_name=default_region)

    # These two one-off network calls can run in parallel.
    fm_list, health_map = await asyncio.gather(
        asyncio.to_thread(build_lifecycle_index, lifecycle_session, verbose),
        asyncio.to_thread(build_health_eol_map, lifecycle_session, verbose)
        if resolve_eol_dates else _empty_dict(),
    )
    card_cache = {}
    card_lock = asyncio.Lock()

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=days)

    # --- shared caches, guarded by locks because rows touch them concurrently ---
    session_cache = {}   # profile -> boto3.Session or None
    client_cache = {}    # (profile, region) -> {"cw","sq"} or None
    quota_cache = {}     # (profile, region) -> [(name, value), ...]
    client_lock = asyncio.Lock()
    quota_lock = asyncio.Lock()

    def _session_for(profile):
        if profile not in session_cache:
            try:
                session_cache[profile] = boto3.Session(profile_name=profile) if profile \
                    else boto3.Session()
            except ProfileNotFound:
                session_cache[profile] = None
        return session_cache[profile]

    async def clients_for(profile, region):
        ck = (profile, region)
        async with client_lock:
            if ck not in client_cache:
                def _build():
                    sess = _session_for(profile)
                    if sess is None:
                        return None
                    return {"cw": sess.client("cloudwatch", region_name=region),
                            "sq": sess.client("service-quotas", region_name=region)}
                client_cache[ck] = await asyncio.to_thread(_build)
            return client_cache[ck]

    async def quotas_for(profile, region):
        qk = (profile, region)
        async with quota_lock:
            if qk not in quota_cache:
                cl = await clients_for(profile, region)
                quota_cache[qk] = await asyncio.to_thread(
                    get_applied_quotas, cl["sq"], verbose) if cl else []
            return quota_cache[qk]

    async def resolve_eol_async(model_id, provider, model_name):
        # Health map lookup is in-memory; only the card scrape may hit the network.
        async with card_lock:
            cached = card_cache.get((provider, model_name), "MISS")
        if cached != "MISS":
            return resolve_eol(model_id, provider, model_name, health_map, card_cache)
        res = await asyncio.to_thread(
            resolve_eol, model_id, provider, model_name, health_map, card_cache, verbose)
        return res

    sem = asyncio.Semaphore(max_concurrency)
    results = {}  # positional index -> dict of enrichment values

    async def process_row(pos, row):
        async with sem:
            model_id = row.get(model_id_col, "")
            notes = []

            fm = match_lifecycle(model_id, fm_list)
            status = fm["status"] if fm else "UNKNOWN"
            provider = fm["provider"] if fm else ""
            model_name = row.get(model_name_col) or (fm["modelName"] if fm else "")
            if not fm:
                notes.append("no lifecycle match")
            is_legacy = status == "LEGACY"

            eol = ""
            if resolve_eol_dates and status in ("LEGACY", "EOL"):
                eol = (await resolve_eol_async(model_id, provider, model_name)) or ""
                if not eol:
                    notes.append("EOL unresolved")

            region = region_for_row(model_id, default_region, geo_defaults)
            _, geo = strip_geo_prefix(model_id)
            if geo == "global":
                notes.append(f"global model: quota/util reflect {region} only")

            account = row.get(account_col, "")
            if account_to_profile:
                profile = account_to_profile.get(account)
                have_creds = profile is not None
            else:
                profile, have_creds = None, True

            applied_rpm = applied_tpm = None
            inv = itok = otok = thr = tpm_peak = None
            if have_creds:
                cl = await clients_for(profile, region)
                if cl is None:
                    notes.append(f"profile '{profile}' not found")
                else:
                    q = await quotas_for(profile, region)
                    applied_rpm = find_quota(model_name, q, "requests")
                    applied_tpm = find_quota(model_name, q, "tokens")
                    cw_id = str(model_id).split("/")[-1]
                    # four CloudWatch calls for this row, concurrently
                    inv, itok, otok, thr, tpm_peak = await asyncio.gather(
                        asyncio.to_thread(metric_sum, cl["cw"], "Invocations", cw_id, start, end, "Sum"),
                        asyncio.to_thread(metric_sum, cl["cw"], "InputTokenCount", cw_id, start, end, "Sum"),
                        asyncio.to_thread(metric_sum, cl["cw"], "OutputTokenCount", cw_id, start, end, "Sum"),
                        asyncio.to_thread(metric_sum, cl["cw"], "InvocationThrottles", cw_id, start, end, "Sum"),
                        asyncio.to_thread(est_tpm_peak, cl["cw"], cw_id, start, end),
                    )
            else:
                notes.append("account not mapped to a profile: quota/util skipped")

            pct = (round(100 * tpm_peak / applied_tpm, 1)
                   if (tpm_peak and applied_tpm) else None)

            results[pos] = {
                "lifecycle_status": status,
                "is_legacy": is_legacy,
                "eol_date": eol,
                "applied_rpm": applied_rpm,
                "applied_tpm": applied_tpm,
                "invocations": None if inv is None else int(inv),
                "input_tokens": None if itok is None else int(itok),
                "output_tokens": None if otok is None else int(otok),
                "throttles": None if thr is None else int(thr),
                "peak_est_tpm_use": round(tpm_peak) if tpm_peak else None,
                "tpm_pct_of_quota": pct,
                "enrich_region": region,
                "enrich_note": "; ".join(notes),
            }

    # positional rows so results map cleanly back regardless of the df's index
    await asyncio.gather(*(process_row(i, row)
                           for i, (_, row) in enumerate(out.iterrows())))

    for col in ENRICH_COLS:
        out[col] = [results[i][col] for i in range(len(out))]
    return out


async def _empty_dict():
    return {}


def enrich_sync(df, **kwargs):
    """
    Blocking wrapper: run enrich(...) to completion and return the dataframe.
    Use from ordinary sync code / notebooks where there's no running event loop.
    (If you're already inside an event loop, await enrich(...) directly instead.)
    """
    return asyncio.run(enrich(df, **kwargs))


# ---------------------------------------------------------------------------
# Example usage (only runs if executed directly)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Minimal demo: read a CSV named on the command- less default, enrich, print.
    # In practice you import enrich() and pass your own dataframe.
    import os
    src = os.environ.get("BEDROCK_INPUT_CSV", "aggregated_report.csv")
    if not os.path.exists(src):
        sys.exit(f"Set BEDROCK_INPUT_CSV or place {src} here. "
                 f"Normally you just `from bedrock_enrich import enrich`.")
    df_in = pd.read_csv(src)
    # Map your account aliases to real AWS profiles here:
    acct_map = {}  # e.g. {"gc1-pdo-np": "my-np-profile"}
    # enrich() is a coroutine; enrich_sync() runs it for plain scripts like this one.
    df_out = enrich_sync(df_in, account_to_profile=acct_map or None, verbose=True)
    out_path = "enriched_" + os.path.basename(src)
    df_out.to_csv(out_path, index=False)
    print(f"Wrote {len(df_out)} rows -> {os.path.abspath(out_path)}")
