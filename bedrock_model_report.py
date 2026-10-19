#!/usr/bin/env python3
"""
bedrock_model_report.py

Consolidated Bedrock model report, scoped to models YOU actually invoke.

For each used model it reports:
  - Utilisation      : invocations, input/output tokens, throttles over a lookback window (CloudWatch)
  - Lifecycle        : Active / Legacy / EOL  (ListFoundationModels -> modelLifecycle.status)
  - EOL date         : if the model is Legacy and AWS has published an EOL date (best-effort, see notes)
  - Legacy flag      : True/False
  - Applied TPM/RPM  : your account's *applied* per-model quotas (Service Quotas), not public defaults
  - Est. TPM quota use: peak EstimatedTPMQuotaUsage over the window, vs applied TPM

"Models I use" = any ModelId that has >0 Invocations in CloudWatch over the lookback window.

Usage:
    python bedrock_model_report.py --region us-east-1 --days 30
    python bedrock_model_report.py --region us-east-1 --days 7 --csv out.csv

Requires: boto3  (pip install boto3). Uses your default AWS credentials/profile.
Needs IAM: bedrock:ListFoundationModels, cloudwatch:ListMetrics, cloudwatch:GetMetricStatistics,
           servicequotas:ListServiceQuotas, servicequotas:GetServiceQuota
"""

import argparse
import csv
import datetime as dt
import sys
from collections import defaultdict

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    sys.exit("boto3 not installed. Run: pip install boto3")

NS = "AWS/Bedrock"


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
    ap = argparse.ArgumentParser(description="Bedrock per-model utilisation + lifecycle + quota report")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--days", type=int, default=30, help="lookback window for utilisation")
    ap.add_argument("--profile", default=None, help="AWS profile name (optional)")
    ap.add_argument("--csv", default=None, help="write results to this CSV path")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region) \
        if args.profile else boto3.Session(region_name=args.region)

    try:
        cw = session.client("cloudwatch")
        bedrock = session.client("bedrock")
        sq = session.client("service-quotas")
    except NoCredentialsError:
        sys.exit("No AWS credentials found. Configure `aws configure` or set --profile.")

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=args.days)

    print(f"Region: {args.region} | Window: last {args.days}d "
          f"({start:%Y-%m-%d} -> {end:%Y-%m-%d} UTC)\n")

    print("Discovering invoked models from CloudWatch...")
    used = get_used_model_ids(cw, start, end)
    if not used:
        print("No invoked models found in this window/region. "
              "Check region, or widen --days. (CloudWatch only shows models with traffic.)")
        return
    print(f"  Found {len(used)} invoked model id(s).\n")

    print("Fetching lifecycle (ListFoundationModels) + applied quotas (Service Quotas)...")
    fm_list = get_lifecycle_map(bedrock)
    quotas = get_applied_quotas(sq)
    print()

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

        applied_rpm = find_quota_for_model(model_name, quotas, "requests")
        applied_tpm = find_quota_for_model(model_name, quotas, "tokens")

        rows.append({
            "model_id": mid,
            "provider": provider,
            "status": status,
            "legacy": is_legacy,
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

    hdr = ["MODEL ID", "STATUS", "LEGACY", "INVOCS", "IN_TOK", "OUT_TOK",
           "THROT", "APPLIED_RPM", "APPLIED_TPM", "PEAK_TPM", "%TPM"]
    print(" | ".join(hdr))
    print("-" * 140)
    for r in rows:
        print(" | ".join([
            r["model_id"][:45],
            r["status"],
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

    legacy_models = [r["model_id"] for r in rows if r["legacy"]]
    if legacy_models:
        print(f"\n⚠  LEGACY models you're still invoking (plan migration before EOL): "
              f"{len(legacy_models)}")
        for m in legacy_models:
            print(f"     - {m}")
        print("   EOL dates are Bedrock-specific; check the Bedrock model-lifecycle page / "
              "your AWS Health Dashboard for the exact date per model.")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows -> {args.csv}")


if __name__ == "__main__":
    main()
