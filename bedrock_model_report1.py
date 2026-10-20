#!/usr/bin/env python3
"""
bedrock_model_report.py

Consolidated Bedrock model report, scoped to models YOU actually invoke.

For each used model it reports:
  - Utilisation      : invocations, input/output tokens, throttles over a lookback window (CloudWatch)
  - Lifecycle        : Active / Legacy / EOL  (ListFoundationModels -> modelLifecycle.status)
  - EOL date         : for Legacy/EOL models, resolved via AWS Health API (primary) then
                       model-card scrape (fallback). Active models have no EOL date by design.
  - Legacy flag      : True/False
  - Applied TPM/RPM  : your account's *applied* per-model quotas (Service Quotas), not public defaults
  - Est. TPM quota use: peak EstimatedTPMQuotaUsage over the window, vs applied TPM

"Models I use" = any ModelId that has >0 Invocations in CloudWatch over the lookback window.

EOL resolution order:
  1. AWS Health API (describe_events/describe_event_details, service=BEDROCK). Pinned to
     us-east-1 (Health's only endpoint). Requires a Business/Enterprise support plan; without
     one it raises SubscriptionRequiredException and we silently fall through.
  2. Model-card page scrape (docs.aws.amazon.com/.../model-card-<provider>-<model>.md|html).
     Post-Sept-2026, EOL dates live on each model card, added when the model enters Legacy.
  Only Legacy/EOL models are resolved (Active models never have a published EOL date).

Usage (no arguments — just run it):
    python bedrock_model_report.py

It prints a table and ALWAYS writes a CSV (auto-named bedrock_report_<region>_<timestamp>.csv
in the current directory).

Optional environment-variable overrides (all have defaults; none required):
    AWS_REGION / AWS_DEFAULT_REGION   region to report on        (default us-east-1)
    AWS_PROFILE                       named credential profile   (default credential chain)
    BEDROCK_REPORT_DAYS               utilisation lookback days  (default 30)
    BEDROCK_REPORT_CSV                exact CSV output path       (default auto-named)
    BEDROCK_REPORT_EOL                "0" to skip EOL resolution (default on)
    BEDROCK_REPORT_VERBOSE            "1" for EOL-source diagnostics (default off)

Requires: boto3  (pip install boto3). Uses your default AWS credentials/profile.
          Model-card fallback uses urllib (stdlib) to reach docs.aws.amazon.com.
Needs IAM: bedrock:ListFoundationModels, cloudwatch:ListMetrics, cloudwatch:GetMetricStatistics,
           servicequotas:ListServiceQuotas, servicequotas:GetServiceQuota,
           health:DescribeEvents, health:DescribeEventDetails  (EOL via Health; optional)
"""

import csv
import datetime as dt
import os
import re
import sys
import urllib.request
from collections import defaultdict

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, EndpointConnectionError
except ImportError:
    sys.exit("boto3 not installed. Run: pip install boto3")

NS = "AWS/Bedrock"
DOCS_BASE = "https://docs.aws.amazon.com/bedrock/latest/userguide"
# Health API is global but only reachable via us-east-1
HEALTH_REGION = "us-east-1"

# Matches "September 30, 2026", "30 September 2026", "2026-09-30"
_DATE_PATTERNS = [
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
    r"\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
    r"\d{4}-\d{2}-\d{2}",
]


def get_used_model_ids(cw, start, end):
    """Find ModelIds with >0 invocations in the window via CloudWatch."""
    # Discover which ModelId dimension values exist for the Invocations metric
    model_ids = set()
    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=NS, MetricName="Invocations"):
        for m in page["Metrics"]:
            for d in m["Dimensions"]:
                if d["Name"] == "ModelId":
                    model_ids.add(d["Value"])
    return sorted(model_ids)


def get_metric_sum(cw, metric, model_id, start, end, stat="Sum"):
    """Sum (or Max) a metric for one ModelId over the window."""
    try:
        resp = cw.get_metric_statistics(
            Namespace=NS,
            MetricName=metric,
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=end,
            Period=86400,  # daily buckets
            Statistics=[stat],
        )
    except ClientError as e:
        return None
    pts = resp.get("Datapoints", [])
    if not pts:
        return 0
    if stat == "Sum":
        return sum(p["Sum"] for p in pts)
    if stat == "Maximum":
        return max(p["Maximum"] for p in pts)
    return 0


def get_est_tpm_peak(cw, model_id, start, end):
    """Peak per-minute EstimatedTPMQuotaUsage for a model (newer metric; may not exist)."""
    try:
        resp = cw.get_metric_statistics(
            Namespace=NS,
            MetricName="EstimatedTPMQuotaUsage",
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=["Maximum"],
        )
    except ClientError:
        return None
    pts = resp.get("Datapoints", [])
    if not pts:
        return None
    return max(p["Maximum"] for p in pts)


def get_lifecycle_map(bedrock):
    """Map base model-id substring -> (status, provider, name). Also return raw list for matching."""
    try:
        resp = bedrock.list_foundation_models()
    except ClientError as e:
        print(f"  ! ListFoundationModels failed: {e}", file=sys.stderr)
        return []
    out = []
    for fm in resp.get("modelSummaries", []):
        lifecycle = fm.get("modelLifecycle", {}) or {}
        out.append({
            "modelId": fm.get("modelId", ""),
            "modelName": fm.get("modelName", ""),
            "provider": fm.get("providerName", ""),
            "status": lifecycle.get("status", "UNKNOWN"),
        })
    return out


def match_lifecycle(used_id, fm_list):
    """
    CloudWatch ModelId may be a bare id, an inference-profile/cross-region id
    (e.g. us.anthropic.claude-...), or an ARN. Strip region prefix + match against FM list.
    """
    candidate = used_id.split("/")[-1]  # strip ARN prefix if present
    # strip cross-region geo prefix: us. / eu. / apac. etc.
    parts = candidate.split(".")
    if len(parts) > 2 and parts[0] in ("us", "eu", "apac", "ap", "ca", "sa"):
        candidate = ".".join(parts[1:])
    for fm in fm_list:
        if fm["modelId"] == candidate or fm["modelId"].startswith(candidate.split(":")[0]):
            return fm
    # looser contains-match
    base = candidate.split(":")[0]
    for fm in fm_list:
        if base and base in fm["modelId"]:
            return fm
    return None


def _extract_date(text):
    """Pull the first date-looking string out of text, or None."""
    if not text:
        return None
    for pat in _DATE_PATTERNS:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return None


def get_eol_from_health(session, verbose=False):
    """
    PRIMARY source. Query AWS Health for Bedrock deprecation/notification events and
    map affected model -> EOL date parsed from the event description.

    Returns {model_key_lower: eol_date_string}. Empty dict on any failure, including
    SubscriptionRequiredException (Health API needs a Business/Enterprise support plan).
    The Health endpoint is global but only lives in us-east-1, so we pin the client there
    regardless of the report region.
    """
    out = {}
    try:
        health = session.client("health", region_name=HEALTH_REGION)
    except Exception as e:
        if verbose:
            print(f"  ! Health client init failed: {e}", file=sys.stderr)
        return out

    try:
        paginator = health.get_paginator("describe_events")
        event_arns = []
        for page in paginator.paginate(
            filter={
                "services": ["BEDROCK"],
                "eventTypeCategories": ["scheduledChange", "accountNotification"],
            }
        ):
            for ev in page.get("events", []):
                event_arns.append(ev["arn"])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "SubscriptionRequiredException":
            if verbose:
                print("  i Health API unavailable (no Business/Enterprise support plan) "
                      "— falling back to model-card scrape.", file=sys.stderr)
        else:
            print(f"  ! Health describe_events failed: {e}", file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"  ! Health describe_events error: {e}", file=sys.stderr)
        return out

    if not event_arns:
        return out

    # Pull detail text for each event (batch of up to 10 per API limits)
    try:
        for i in range(0, len(event_arns), 10):
            batch = event_arns[i:i + 10]
            details = health.describe_event_details(eventArns=batch)
            for d in details.get("successfulSet", []):
                desc = d.get("eventDescription", {}).get("latestDescription", "") or ""
                eol = _extract_date(desc)
                if not eol:
                    continue
                # Harvest any model identifiers mentioned in the description.
                # Bedrock deprecation notices name the model id, e.g. anthropic.claude-3-...
                for tok in re.findall(r"[a-z0-9]+\.[a-z0-9][a-z0-9\-.:]+", desc.lower()):
                    out[tok] = eol
                # also index by human model name tokens if present
    except Exception as e:
        if verbose:
            print(f"  ! Health describe_event_details error: {e}", file=sys.stderr)

    return out


def _card_url_candidates(provider, model_name):
    """
    Build likely model-card URLs. Card slugs look like:
      model-card-anthropic-claude-3-haiku.html  (+ a .md variant for clean parsing)
    We slugify provider + name and try the .md first (easier to parse), then .html.
    """
    def slug(s):
        s = s.lower().strip()
        s = s.replace("+", "-plus").replace(".", "-").replace(" ", "-")
        s = re.sub(r"[^a-z0-9\-]", "", s)
        s = re.sub(r"-+", "-", s).strip("-")
        return s

    prov = slug(provider)
    name = slug(model_name)
    if not prov or not name:
        return []
    stem = f"model-card-{prov}-{name}"
    return [f"{DOCS_BASE}/{stem}.md", f"{DOCS_BASE}/{stem}.html"]


def get_eol_from_card(provider, model_name, cache, verbose=False):
    """
    FALLBACK source. Fetch the model's own card page and extract the EOL date.
    Post-Sept-2026 policy: EOL dates live on each model card, not a central table,
    and are only present once the model has entered Legacy.
    Returns an EOL date string or None. Results cached per (provider, model_name).
    """
    key = (provider, model_name)
    if key in cache:
        return cache[key]

    result = None
    for url in _card_url_candidates(provider, model_name):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bedrock-report/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        # Look for a line that mentions EOL / end-of-life and grab the nearby date.
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
    if verbose and result:
        print(f"  i Card EOL for {provider} {model_name}: {result}", file=sys.stderr)
    return result


def resolve_eol(session, health_map, provider, model_name, model_id, card_cache, verbose=False):
    """
    Try Health map first (keyed by any model-id token found in the notice), then card scrape.
    Returns an EOL date string or None.
    """
    # Health map is keyed by lowercased model-id tokens; test the used id and its base
    base = model_id.split("/")[-1].lower()
    for key in (base, base.split(":")[0]):
        if key in health_map:
            return health_map[key]
    # try partial containment against health keys
    for hk, hv in health_map.items():
        if hk and (hk in base or base in hk):
            return hv
    # fallback: scrape the model card
    return get_eol_from_card(provider, model_name, card_cache, verbose=verbose)


def get_applied_quotas(sq):
    """
    Pull Bedrock service quotas and index per-model TPM/RPM 'applied' values.
    Quota names look like:
      'On-demand model inference requests per minute for Anthropic Claude 3.5 Sonnet'
      'On-demand model inference tokens per minute for Anthropic Claude 3.5 Sonnet'
    Returns list of (quota_name, applied_value).
    """
    quotas = []
    try:
        paginator = sq.get_paginator("list_service_quotas")
        for page in paginator.paginate(ServiceCode="bedrock"):
            for q in page["Quotas"]:
                quotas.append((q["QuotaName"], q.get("Value")))
    except ClientError as e:
        print(f"  ! Service Quotas lookup failed (need servicequotas:ListServiceQuotas): {e}",
              file=sys.stderr)
    return quotas


def find_quota_for_model(model_name, quotas, kind):
    """
    kind = 'requests' or 'tokens'. Fuzzy-match a model's human name against quota names.
    Returns applied value or None. Bedrock quota names embed the model's display name.
    """
    if not model_name:
        return None
    key = "requests per minute" if kind == "requests" else "tokens per minute"
    name_tokens = [t.lower() for t in model_name.replace("-", " ").split() if len(t) > 2]
    best = None
    for qname, qval in quotas:
        ql = qname.lower()
        if key not in ql:
            continue
        if "on-demand" not in ql and "cross-region" not in ql:
            continue
        # require most of the model-name tokens to appear
        hits = sum(1 for t in name_tokens if t in ql)
        if name_tokens and hits >= max(1, len(name_tokens) - 1):
            if best is None or (qval or 0) > (best or 0):
                best = qval
    return best


def main():
    # ---- configuration (no command-line arguments needed) ----
    # Override any of these via environment variables if you want, but nothing is required.
    REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    DAYS = int(os.environ.get("BEDROCK_REPORT_DAYS", "30"))   # utilisation lookback window
    PROFILE = os.environ.get("AWS_PROFILE")                   # None -> default credential chain
    RESOLVE_EOL = os.environ.get("BEDROCK_REPORT_EOL", "1") != "0"
    VERBOSE = os.environ.get("BEDROCK_REPORT_VERBOSE", "0") == "1"
    # CSV always written; auto-named with region + timestamp unless BEDROCK_REPORT_CSV set.
    csv_path = os.environ.get("BEDROCK_REPORT_CSV") or \
        f"bedrock_report_{REGION}_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"

    session = boto3.Session(profile_name=PROFILE, region_name=REGION) \
        if PROFILE else boto3.Session(region_name=REGION)

    try:
        cw = session.client("cloudwatch")
        bedrock = session.client("bedrock")
        sq = session.client("service-quotas")
    except NoCredentialsError:
        sys.exit("No AWS credentials found. Run `aws configure`, or set AWS_PROFILE / "
                 "standard AWS env vars.")

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=DAYS)

    print(f"Region: {REGION} | Window: last {DAYS}d "
          f"({start:%Y-%m-%d} -> {end:%Y-%m-%d} UTC)\n")

    print("Discovering invoked models from CloudWatch...")
    used = get_used_model_ids(cw, start, end)
    if not used:
        print("No invoked models found in this window/region. "
              "Try a different region, or widen the window via BEDROCK_REPORT_DAYS. "
              "(CloudWatch only shows models with traffic.)")
        # still write an empty CSV so the output file always exists
        with open(csv_path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=[
                "model_id", "provider", "status", "legacy", "eol_date",
                "invocations", "input_tokens", "output_tokens", "throttles",
                "applied_rpm", "applied_tpm", "peak_est_tpm_use", "tpm_headroom"
            ]).writeheader()
        print(f"Wrote 0 rows (header only) -> {os.path.abspath(csv_path)}")
        return
    print(f"  Found {len(used)} invoked model id(s).\n")

    print("Fetching lifecycle (ListFoundationModels) + applied quotas (Service Quotas)...")
    fm_list = get_lifecycle_map(bedrock)
    quotas = get_applied_quotas(sq)

    health_map = {}
    if RESOLVE_EOL:
        print("Resolving EOL dates (AWS Health API -> model-card fallback)...")
        health_map = get_eol_from_health(session, verbose=VERBOSE)
        if health_map:
            print(f"  Health API returned {len(health_map)} model->date hint(s).")
    print()

    card_cache = {}
    rows = []
    for mid in used:
        inv = get_metric_sum(cw, "Invocations", mid, start, end, "Sum") or 0
        if inv == 0:
            continue  # not actually used in window
        itok = get_metric_sum(cw, "InputTokenCount", mid, start, end, "Sum") or 0
        otok = get_metric_sum(cw, "OutputTokenCount", mid, start, end, "Sum") or 0
        thr = get_metric_sum(cw, "InvocationThrottles", mid, start, end, "Sum") or 0
        tpm_peak = get_est_tpm_peak(cw, mid, start, end)

        fm = match_lifecycle(mid, fm_list)
        status = fm["status"] if fm else "UNKNOWN"
        model_name = fm["modelName"] if fm else ""
        provider = fm["provider"] if fm else ""
        is_legacy = status == "LEGACY"

        # EOL date only exists once a model is Legacy (or already EOL). Active -> None by design.
        eol_date = None
        if RESOLVE_EOL and status in ("LEGACY", "EOL"):
            eol_date = resolve_eol(session, health_map, provider, model_name, mid,
                                   card_cache, verbose=VERBOSE)

        applied_rpm = find_quota_for_model(model_name, quotas, "requests")
        applied_tpm = find_quota_for_model(model_name, quotas, "tokens")

        rows.append({
            "model_id": mid,
            "provider": provider,
            "status": status,
            "legacy": is_legacy,
            "eol_date": eol_date or "",
            "invocations": int(inv),
            "input_tokens": int(itok),
            "output_tokens": int(otok),
            "throttles": int(thr),
            "applied_rpm": applied_rpm,
            "applied_tpm": applied_tpm,
            "peak_est_tpm_use": round(tpm_peak) if tpm_peak else None,
            "tpm_headroom": (round(100 * tpm_peak / applied_tpm, 1)
                             if (tpm_peak and applied_tpm) else None),
        })

    rows.sort(key=lambda r: r["invocations"], reverse=True)

    # ---- print table ----
    def fmt(v):
        return "-" if v is None else (f"{v:,}" if isinstance(v, int) else str(v))

    hdr = ["MODEL ID", "STATUS", "EOL DATE", "LEGACY", "INVOCS", "IN_TOK", "OUT_TOK",
           "THROT", "APPLIED_RPM", "APPLIED_TPM", "PEAK_TPM", "%TPM"]
    print(" | ".join(hdr))
    print("-" * 160)
    for r in rows:
        print(" | ".join([
            r["model_id"][:45],
            r["status"],
            r["eol_date"] or "-",
            "YES" if r["legacy"] else "no",
            fmt(r["invocations"]),
            fmt(r["input_tokens"]),
            fmt(r["output_tokens"]),
            fmt(r["throttles"]),
            fmt(r["applied_rpm"]),
            fmt(r["applied_tpm"]),
            fmt(r["peak_est_tpm_use"]),
            f'{r["tpm_headroom"]}%' if r["tpm_headroom"] is not None else "-",
        ]))

    legacy_rows = [r for r in rows if r["legacy"] or r["status"] == "EOL"]
    if legacy_rows:
        print(f"\n⚠  LEGACY/EOL models you're still invoking (migrate before the EOL date): "
              f"{len(legacy_rows)}")
        for r in legacy_rows:
            date = r["eol_date"] or "date unresolved — check model card / Health Dashboard"
            print(f"     - {r['model_id']}  →  EOL: {date}")
        missing = [r for r in legacy_rows if not r["eol_date"]]
        if missing:
            print("   Some EOL dates couldn't be resolved automatically. The AWS Health API "
                  "needs a Business/Enterprise support plan; otherwise dates come from each\n"
                  "   model's card page and may lag. Verify against your AWS Health Dashboard.")

    # ---- always write CSV ----
    fieldnames = ["model_id", "provider", "status", "legacy", "eol_date",
                  "invocations", "input_tokens", "output_tokens", "throttles",
                  "applied_rpm", "applied_tpm", "peak_est_tpm_use", "tpm_headroom"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} row(s) -> {os.path.abspath(csv_path)}")


if __name__ == "__main__":
    main()
