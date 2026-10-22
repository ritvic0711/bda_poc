#!/usr/bin/env python3
"""
bedrock_enrich.py

Enrich an existing Bedrock model dataframe with lifecycle status, EOL date, and
per-model TPM/RPM quotas + utilisation.

You already have a dataframe (like the aggregated_report sheet) with columns such as
"Model Name", "Model ID", "Environment", "Account", "Vendor", "Avg Latency", ...
This module ADDS columns to it; it does not query "which models do I use" — your df
already is that list.

Public API:
    enrich(df, ...) -> df          # synchronous; returns a NEW dataframe with enrichment cols

Rows are processed sequentially. The one-off ListFoundationModels / Health calls run once
up front; per-(profile, region) clients + quota lists and per-model card/EOL lookups are
cached so nothing is refetched.

Columns added:
    lifecycle_status   ACTIVE / LEGACY / EOL / UNKNOWN   (ListFoundationModels, else "")
    is_legacy          True/False
    eol_date           for LEGACY/EOL models (Health API -> model-card scrape); else ""
    inference_type     'cross-region' (geo-prefixed/global ids) | 'on-demand' (bare id)
    allocated_rpm      your account's allocated requests-per-minute quota (Service Quotas)
    allocated_tpm      your account's allocated tokens-per-minute quota, for the quota that
                       actually governs this call (Cross-Region TPM vs On-demand TPM)

This reports ALLOCATED QUOTAS + LIFECYCLE only. It makes no CloudWatch calls and reports no
usage/utilisation figures. (Pass verbose=True to see per-row diagnostics on stderr — why a
quota came back blank, region assumptions, etc.)

Data sources:
  - Lifecycle + EOL: live ListFoundationModels (authoritative status), with the Bedrock
    model-card page scrape as the EOL-date fallback (the API returns status, not the date).
    These need only bedrock:ListFoundationModels (any one working credential), so they run
    once globally, not per account.
  - Allocated TPM/RPM: live Service Quotas per row, using the row's Account -> AWS profile
    mapping and the region inferred from the Model ID prefix. Rows with no mapped profile get
    lifecycle/EOL but blank quotas (noted in enrich_note), rather than failing.

Region inference from Model ID prefix:
    us.*  / eu.* / apac.*  -> that geo's default region (overridable via geo_default_region)
    global.*               -> NOT parseable (global routing); uses default_region, and the
                              utilisation/quota figures then reflect only that one region.
    bare id (no prefix)    -> default_region

Requires: boto3, pandas. AWS creds via the profiles you map (or the default chain).
IAM: bedrock:ListFoundationModels, servicequotas:ListServiceQuotas
     (+ health:DescribeEvents* optional for EOL dates).
"""

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


def inference_type_for_row(model_id):
    """
    How the model is invoked, which decides WHICH TPM quota governs it.
    Any geo prefix (us./eu./apac./global./...) means the call goes through a cross-Region
    inference profile -> governed by the Cross-Region TPM quota. A bare id is on-demand,
    single-Region -> governed by the On-demand TPM quota.
    Returns 'cross-region' or 'on-demand'.
    """
    _, geo = strip_geo_prefix(model_id)
    return "on-demand" if geo is None else "cross-region"


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


# Tokens common to many model names; matching these alone must not qualify a quota.
_GENERIC_NAME_TOKENS = {"claude", "anthropic", "amazon", "titan", "nova", "llama",
                        "meta", "mistral", "cohere", "command", "instruct", "model",
                        "the", "for", "ai", "gpt", "openai"}


def _name_matches(model_name, quota_name_lower):
    """
    True only if the model's DISTINCTIVE tokens all appear in the quota name.
    We drop generic tokens (claude/anthropic/...) from the required set so that
    e.g. 'Claude Opus 4 8' does NOT match a 'Claude 3 Haiku' quota just on 'claude'.
    Every distinctive token (version numbers, 'haiku', 'sonnet', 'opus', ...) must be present.
    """
    toks = [t.lower() for t in str(model_name).replace("-", " ").split() if len(t) > 2]
    if not toks:
        return False
    distinctive = [t for t in toks if t not in _GENERIC_NAME_TOKENS]
    if not distinctive:
        # name is all-generic (rare) -> fall back to requiring all tokens
        return all(t in quota_name_lower for t in toks)
    # all distinctive tokens must appear; allow at most one miss only if there are many
    hits = sum(1 for t in distinctive if t in quota_name_lower)
    return hits == len(distinctive)


def find_quota(model_name, quotas, kind, inference_type):
    """
    Return (value, matched_quota_name) for the quota that actually governs this call,
    or (None, None).

    kind           : 'requests' (RPM) | 'tokens' (TPM)
    inference_type : 'cross-region' | 'on-demand'  -- picks the right TPM quota.

    Bedrock quota naming (per docs):
      TPM: 'Cross-Region InvokeModel tokens per minute for <model>'
           'On-demand InvokeModel tokens per minute for <model>'   (two SEPARATE limits)
      RPM: 'InvokeModel requests per minute for <model>'           (single quota, no split;
           and some models have NO rpm quota at all, e.g. Opus 4.7/4.8)
    Older accounts may still show the legacy '... model inference ...' wording, so we accept
    either 'invokemodel' or 'model inference' in the name.
    """
    if not model_name:
        return (None, None)
    key = "requests per minute" if kind == "requests" else "tokens per minute"
    want_cross = inference_type == "cross-region"

    exact = None          # correct scope for this call
    fallback = None       # any scope, used only if the exact-scope quota is absent
    for qn, qv in quotas:
        ql = qn.lower()
        if key not in ql:
            continue
        if "invokemodel" not in ql and "model inference" not in ql:
            continue
        if not _name_matches(model_name, ql):
            continue

        is_cross = "cross-region" in ql or "cross region" in ql
        is_ondemand = "on-demand" in ql or "on demand" in ql

        if kind == "tokens":
            # TPM is scope-specific: prefer the quota matching this call's inference type.
            if (want_cross and is_cross) or ((not want_cross) and is_ondemand):
                if exact is None or (qv or 0) > (exact[0] or 0):
                    exact = (qv, qn)
            if fallback is None or (qv or 0) > (fallback[0] or 0):
                fallback = (qv, qn)
        else:
            # RPM: single un-scoped quota in current naming; take the name-matched one.
            if exact is None or (qv or 0) > (exact[0] or 0):
                exact = (qv, qn)

    chosen = exact or fallback
    return chosen if chosen else (None, None)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
ENRICH_COLS = (
    "lifecycle_status", "is_legacy", "eol_date",
    "inference_type", "allocated_rpm", "allocated_tpm")


def enrich(df,
           model_id_col="Model ID",
           model_name_col="Model Name",
           account_col="Account",
           account_to_profile=None,
           default_region="us-east-1",
           geo_default_region=None,
           resolve_eol_dates=True,
           lifecycle_session=None,
           verbose=False):
    """
    Return a NEW dataframe = df + enrichment columns (see module docstring).

    Plain synchronous function: processes rows one at a time. The one-off
    ListFoundationModels / Health calls run once up front; per-(profile, region) quota lists
    and per-model card/EOL lookups are cached so nothing is refetched. No CloudWatch calls —
    this reports allocated quotas + lifecycle only, not usage.

    Parameters
    ----------
    df : pandas.DataFrame            your aggregated report.
    model_id_col / model_name_col / account_col : column names in df.
    account_to_profile : dict        {account_alias: aws_profile_name}. Rows whose account
                                     isn't mapped get lifecycle/EOL but blank quotas.
                                     If None, the default credential chain is used for ALL
                                     rows (fine when every row is one account you're authed to).
    default_region : str             region used for bare ids and global.* models.
    geo_default_region : dict        override GEO_DEFAULT_REGION, e.g. {"eu": "eu-central-1"}.
    resolve_eol_dates : bool         set False to skip the EOL lookup entirely.
    lifecycle_session : boto3.Session creds for the one global ListFoundationModels / Health
                                     call. Defaults to the first mapped profile, else default chain.
    """
    if model_id_col not in df.columns:
        raise KeyError(f"'{model_id_col}' not in dataframe columns: {list(df.columns)}")

    out = df.copy()
    account_to_profile = account_to_profile or {}
    geo_defaults = {**GEO_DEFAULT_REGION, **(geo_default_region or {})}

    # --- one global session for lifecycle/EOL (creds-light) ---
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

    fm_list = build_lifecycle_index(lifecycle_session, verbose=verbose)
    health_map = build_health_eol_map(lifecycle_session, verbose=verbose) \
        if resolve_eol_dates else {}
    card_cache = {}

    # --- caches, built once each ---
    session_cache = {}   # profile -> boto3.Session or None
    sq_cache = {}        # (profile, region) -> service-quotas client or None
    quota_cache = {}     # (profile, region) -> [(name, value), ...]

    def session_for(profile):
        if profile not in session_cache:
            try:
                session_cache[profile] = boto3.Session(profile_name=profile) if profile \
                    else boto3.Session()
            except ProfileNotFound:
                session_cache[profile] = None
        return session_cache[profile]

    def sq_client_for(profile, region):
        ck = (profile, region)
        if ck not in sq_cache:
            sess = session_for(profile)
            sq_cache[ck] = sess.client("service-quotas", region_name=region) if sess else None
        return sq_cache[ck]

    def quotas_for(profile, region):
        qk = (profile, region)
        if qk not in quota_cache:
            sq = sq_client_for(profile, region)
            quota_cache[qk] = get_applied_quotas(sq, verbose=verbose) if sq else None
        return quota_cache[qk]

    cols = {k: [] for k in ENRICH_COLS}

    for _, row in out.iterrows():
        model_id = row.get(model_id_col, "")
        notes = []

        fm = match_lifecycle(model_id, fm_list)
        status = fm["status"] if fm else "UNKNOWN"
        provider = fm["provider"] if fm else ""
        # Display name for quota matching: prefer the API's human name ("Claude 3 Haiku").
        # The sheet's "Model Name" is often an ID form (anthropic.claude-3-haiku), which
        # has no distinctive display tokens, so fall back to it only when the API gave none.
        df_name = row.get(model_name_col) or ""
        api_name = fm["modelName"] if fm else ""
        model_name = api_name or df_name
        if api_name and "." in str(df_name):
            model_name = api_name
        if not fm:
            notes.append("no lifecycle match")
        is_legacy = status == "LEGACY"

        eol = ""
        if resolve_eol_dates and status in ("LEGACY", "EOL"):
            eol = resolve_eol(model_id, provider, model_name, health_map, card_cache,
                              verbose=verbose) or ""
            if not eol:
                notes.append("EOL unresolved")

        region = region_for_row(model_id, default_region, geo_defaults)
        _, geo = strip_geo_prefix(model_id)
        inference_type = inference_type_for_row(model_id)
        if geo == "global":
            notes.append(f"global model: quotas reflect {region} only")

        account = row.get(account_col, "")
        if account_to_profile:
            profile = account_to_profile.get(account)
            have_creds = profile is not None
        else:
            profile, have_creds = None, True

        allocated_rpm = allocated_tpm = None
        if have_creds:
            q = quotas_for(profile, region)
            if q is None:
                notes.append(f"profile '{profile}' not found")
            else:
                allocated_rpm, _ = find_quota(model_name, q, "requests", inference_type)
                allocated_tpm, tpm_quota_name = find_quota(
                    model_name, q, "tokens", inference_type)
                if tpm_quota_name and inference_type == "cross-region" \
                        and "cross" not in tpm_quota_name.lower():
                    notes.append("no cross-region TPM quota; showing on-demand")
                if allocated_rpm is None:
                    notes.append("no RPM quota (some models have none)")
                if allocated_tpm is None:
                    notes.append("no TPM quota matched")
        else:
            notes.append("account not mapped to a profile: quotas skipped")

        # `notes` and `region` are still used internally (region picks the quota region,
        # notes feed verbose logging) but are no longer emitted as columns.
        if verbose and notes:
            print(f"  [{model_id}] {'; '.join(notes)}", file=sys.stderr)

        cols["lifecycle_status"].append(status)
        cols["is_legacy"].append(is_legacy)
        cols["eol_date"].append(eol)
        cols["inference_type"].append(inference_type)
        cols["allocated_rpm"].append(allocated_rpm)
        cols["allocated_tpm"].append(allocated_tpm)

    for col in ENRICH_COLS:
        out[col] = cols[col]
    return out


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
    df_out = enrich(df_in, account_to_profile=acct_map or None, verbose=True)
    out_path = "enriched_" + os.path.basename(src)
    df_out.to_csv(out_path, index=False)
    print(f"Wrote {len(df_out)} rows -> {os.path.abspath(out_path)}")
