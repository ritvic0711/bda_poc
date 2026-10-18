#!/usr/bin/env python3
"""
bedrock_full_report.py — One report combining everything Bedrock exposes about a
model's throughput + usage, from the THREE separate sources that actually hold it:

  1. bedrock (control plane)  -> model metadata, provisioned-throughput config (Model Units)
  2. cloudwatch               -> real utilization & token counts (Invocations, tokens, throttles)
  3. static lookup table      -> context window + max output tokens (NOT in any API)

Terminology note:
  - "PTU" is Azure OpenAI's term. Bedrock's equivalent is PROVISIONED THROUGHPUT,
    billed in MODEL UNITS (MUs). This script reports committed MUs (config) AND
    the CloudWatch metrics that tell you how hard they're driven (utilization).
  - Context window / token limits are NOT returned by any Bedrock API. The table
    below is static and must be maintained by hand.

Usage:
    python bedrock_full_report.py --model-id anthropic.claude-3-5-sonnet-20241022-v2:0
    python bedrock_full_report.py --model-id <id> --region us-east-1 --days 30
    python bedrock_full_report.py --profile us.anthropic.claude-3-5-sonnet-20241022-v2:0

Requires boto3 + creds with:
    bedrock:GetFoundationModel, bedrock:GetInferenceProfile,
    bedrock:ListProvisionedModelThroughputs, cloudwatch:GetMetricStatistics
"""

import argparse
import json
import sys
from datetime import datetime, date, timedelta, timezone

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, EndpointConnectionError


# -------------------------------------------------------------------------
# Static lookup: context window (input) + max output tokens.
# NOT available from any Bedrock API — maintain this by hand.
# Keys match on substring of the modelId, so profile-prefixed IDs also hit.
# Numbers are AWS/provider published values; verify against current docs.
# -------------------------------------------------------------------------
TOKEN_LIMITS = {
    "anthropic.claude-3-5-sonnet":   {"context_window": 200_000, "max_output": 8_192},
    "anthropic.claude-3-5-haiku":    {"context_window": 200_000, "max_output": 8_192},
    "anthropic.claude-3-opus":       {"context_window": 200_000, "max_output": 4_096},
    "anthropic.claude-3-sonnet":     {"context_window": 200_000, "max_output": 4_096},
    "anthropic.claude-3-haiku":      {"context_window": 200_000, "max_output": 4_096},
    "anthropic.claude-sonnet-4":     {"context_window": 200_000, "max_output": 64_000},
    "anthropic.claude-opus-4":       {"context_window": 200_000, "max_output": 32_000},
    "anthropic.claude-v2":           {"context_window": 100_000, "max_output": 4_096},
    "amazon.titan-text-express":     {"context_window": 8_192,   "max_output": 8_192},
    "amazon.titan-text-lite":        {"context_window": 4_096,   "max_output": 4_096},
    "amazon.nova-pro":               {"context_window": 300_000, "max_output": 5_120},
    "amazon.nova-lite":              {"context_window": 300_000, "max_output": 5_120},
    "amazon.nova-micro":             {"context_window": 128_000, "max_output": 5_120},
    "meta.llama3-1-8b":              {"context_window": 128_000, "max_output": 2_048},
    "meta.llama3-1-70b":             {"context_window": 128_000, "max_output": 2_048},
    "meta.llama3-1-405b":            {"context_window": 128_000, "max_output": 2_048},
    "mistral.mistral-large":         {"context_window": 128_000, "max_output": 8_192},
    "cohere.command-r-plus":         {"context_window": 128_000, "max_output": 4_096},
    "cohere.command-r":              {"context_window": 128_000, "max_output": 4_096},
}


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def lookup_token_limits(model_id):
    for key, vals in TOKEN_LIMITS.items():
        if key in (model_id or ""):
            return vals
    return None


def resolve_profile_to_model(bedrock, profile_id):
    """Return the first underlying model ID for an inference profile."""
    try:
        prof = bedrock.get_inference_profile(inferenceProfileIdentifier=profile_id)
    except ClientError as e:
        sys.exit(f"[error] get_inference_profile failed: {e.response['Error']['Message']}")
    models = prof.get("models", [])
    if not models:
        sys.exit("[error] profile references no models.")
    arn = models[0]["modelArn"]
    return arn.split("/", 1)[1] if "/" in arn else arn, prof


def get_model_metadata(bedrock, model_id):
    try:
        return bedrock.get_foundation_model(modelIdentifier=model_id).get("modelDetails", {})
    except ClientError as e:
        return {"__error__": e.response["Error"]["Message"]}


def find_provisioned_throughput(bedrock, model_id):
    """Scan provisioned-throughput commitments for ones backing this model.
    Returns [] if none — i.e. you're on-demand / inference-profile (no committed MUs)."""
    found = []
    try:
        paginator = bedrock.get_paginator("list_provisioned_model_throughputs")
        for page in paginator.paginate():
            for pt in page.get("provisionedModelSummaries", []):
                backing = pt.get("foundationModelArn", "") + pt.get("modelArn", "")
                if model_id in backing:
                    found.append({
                        "provisionedModelName": pt.get("provisionedModelName"),
                        "provisionedModelArn":  pt.get("provisionedModelArn"),
                        "modelUnits":           pt.get("modelUnits"),
                        "desiredModelUnits":    pt.get("desiredModelUnits"),
                        "status":               pt.get("status"),
                        "commitmentDuration":   pt.get("commitmentDuration"),
                        "commitmentExpiration": pt.get("commitmentExpirationTime"),
                    })
    except ClientError:
        pass
    return found


def get_cloudwatch_usage(region, model_id, days):
    """Pull invocation + token metrics for the model from the AWS/Bedrock namespace."""
    cw = boto3.client("cloudwatch", region_name=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    period = 86_400 if days >= 7 else 3_600  # daily vs hourly granularity

    metrics = {
        "Invocations":              "Sum",
        "InvocationThrottles":      "Sum",
        "InvocationClientErrors":   "Sum",
        "InvocationServerErrors":   "Sum",
        "InputTokenCount":          "Sum",
        "OutputTokenCount":         "Sum",
        "InvocationLatency":        "Average",
    }
    out = {}
    for metric, stat in metrics.items():
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/Bedrock",
                MetricName=metric,
                Dimensions=[{"Name": "ModelId", "Value": model_id}],
                StartTime=start, EndTime=end,
                Period=period, Statistics=[stat],
            )
            pts = resp.get("Datapoints", [])
            if stat == "Sum":
                out[metric] = sum(p["Sum"] for p in pts)
            else:
                out[metric] = (sum(p["Average"] for p in pts) / len(pts)) if pts else 0
        except ClientError as e:
            out[metric] = f"error: {e.response['Error']['Message']}"
    return out


def report(model_id, meta, limits, prov, usage, days, profile=None):
    print("=" * 72)
    print(f"BEDROCK FULL REPORT — {model_id}")
    print("=" * 72)

    if profile:
        print(f"\n[via inference profile] {profile.get('inferenceProfileId')} "
              f"({profile.get('type')}, {profile.get('status')})")

    print("\n--- Model metadata (bedrock API) ---")
    if meta.get("__error__"):
        print(f"  (!) {meta['__error__']}")
    else:
        lc = meta.get("modelLifecycle", {})
        print(f"  Provider           : {meta.get('providerName')}")
        print(f"  Lifecycle          : {lc.get('status')}  (LEGACY = deprecated/EOL)")
        print(f"  Input modalities   : {', '.join(meta.get('inputModalities', []) or [])}")
        print(f"  Output modalities  : {', '.join(meta.get('outputModalities', []) or [])}")
        print(f"  Inference types    : {', '.join(meta.get('inferenceTypesSupported', []) or [])}")
        print(f"  Customizations     : {', '.join(meta.get('customizationsSupported', []) or [])}")
        print(f"  Streaming          : {meta.get('responseStreamingSupported')}")

    print("\n--- Context window & token limits (STATIC table, not an API) ---")
    if limits:
        print(f"  Context window     : {limits['context_window']:,} tokens")
        print(f"  Max output tokens  : {limits['max_output']:,} tokens")
    else:
        print("  (no entry in lookup table — add this model to TOKEN_LIMITS)")

    print("\n--- Provisioned throughput / Model Units (the Bedrock 'PTU') ---")
    if prov:
        for p in prov:
            print(f"  {p['provisionedModelName']}")
            print(f"    Model units      : {p['modelUnits']} (desired: {p['desiredModelUnits']})")
            print(f"    Status           : {p['status']}")
            print(f"    Commitment       : {p['commitmentDuration']} "
                  f"(expires {p['commitmentExpiration']})")
    else:
        print("  None committed → you are ON-DEMAND / inference-profile.")
        print("  There is no 'PTU utilization %' in this mode; see CloudWatch below for load.")

    print(f"\n--- Utilization & tokens (CloudWatch, last {days} days) ---")
    def fmt(v): return f"{v:,.0f}" if isinstance(v, (int, float)) else v
    print(f"  Total invocations  : {fmt(usage.get('Invocations'))}")
    print(f"  Input tokens       : {fmt(usage.get('InputTokenCount'))}")
    print(f"  Output tokens      : {fmt(usage.get('OutputTokenCount'))}")
    print(f"  Throttles          : {fmt(usage.get('InvocationThrottles'))}  "
          "(high + provisioned = under-provisioned MUs)")
    print(f"  Client errors      : {fmt(usage.get('InvocationClientErrors'))}")
    print(f"  Server errors      : {fmt(usage.get('InvocationServerErrors'))}")
    lat = usage.get('InvocationLatency')
    print(f"  Avg latency (ms)   : {fmt(lat)}")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser(description="Combined Bedrock throughput + usage report.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--model-id", help="Foundation model ID")
    g.add_argument("--profile", help="Inference profile ID/ARN (resolved to its model)")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--days", type=int, default=30, help="CloudWatch window (default 30)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        bedrock = boto3.client("bedrock", region_name=args.region)

        profile = None
        if args.profile:
            model_id, profile = resolve_profile_to_model(bedrock, args.profile)
        else:
            model_id = args.model_id

        meta   = get_model_metadata(bedrock, model_id)
        limits = lookup_token_limits(model_id)
        prov   = find_provisioned_throughput(bedrock, model_id)
        usage  = get_cloudwatch_usage(args.region, model_id, args.days)

        if args.json:
            print(json.dumps({
                "modelId": model_id, "profile": profile, "metadata": meta,
                "tokenLimits": limits, "provisionedThroughput": prov,
                "cloudwatchUsage": usage, "windowDays": args.days,
            }, indent=2, default=_json_default))
        else:
            report(model_id, meta, limits, prov, usage, args.days, profile)

    except NoCredentialsError:
        sys.exit("[error] No AWS credentials. Run `aws configure`.")
    except EndpointConnectionError:
        sys.exit(f"[error] Could not reach region '{args.region}'.")


if __name__ == "__main__":
    main()
