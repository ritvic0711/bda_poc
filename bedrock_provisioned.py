#!/usr/bin/env python3
"""
bedrock_provisioned.py

Standalone enrichment pass for Amazon Bedrock Provisioned Throughput (reserved
capacity), designed to run on its own from a Jenkins pipeline.

Reserved capacity is NOT a service quota. It is purchased capacity measured in
model units (MUs), bought against a commitment term (None / 1mo / 6mo), and it
comes from the `bedrock` client's list_provisioned_model_throughputs API, not
from service-quotas. So this runs separately from the RPM/TPM quota enrichment
and only adds three columns:

    allocated_mus     total model units provisioned for the row's base model
    commitment_term   "None" | "OneMonth" | "SixMonth" | "mixed"
    pt_status         "InService" | "Creating" | ... | "" (none provisioned)

These are additive: a model can have provisioned capacity AND on-demand /
cross-region access at the same time, so a blank here just means "no reserved
capacity for this model in this region", not "no access".

Usage (what Jenkins calls):
    python3 bedrock_provisioned.py \
        --input  model_report.csv \
        --output model_report_provisioned.csv \
        --default-region us-east-1 \
        --account-to-profile accounts.json \
        --verbose

Exit codes (so a pipeline can gate on them):
    0  success (rows written; matches may be zero unless --fail-on-empty)
    2  bad arguments / input file missing / input unreadable
    3  no AWS credentials usable for any row (nothing could be enriched)
    4  --fail-on-empty set and zero rows matched a provisioned throughput
"""

import argparse
import json
import os
import sys

import boto3
import pandas as pd
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

# --- reuse the geo-prefix parser from the main module if it's importable,
#     otherwise fall back to a local copy so this script runs fully standalone.
try:
    from bedrock_enrich import strip_geo_prefix  # type: ignore
except Exception:  # ImportError, or module not on path in the Jenkins workspace
    GEO_PREFIXES = {"us", "eu", "apac", "ap", "global", "ca", "sa"}

    def strip_geo_prefix(model_id):
        """('us.anthropic.claude-sonnet-5', ...) -> ('anthropic.claude-sonnet-5', 'us')."""
        if not isinstance(model_id, str) or not model_id:
            return "", None
        base = model_id.strip().split("/")[-1]  # drop ARN prefix if any
        parts = base.split(".")
        if len(parts) > 2 and parts[0] in GEO_PREFIXES:
            return ".".join(parts[1:]), parts[0]
        return base, None


GEO_DEFAULT_REGION = {
    "us": "us-east-1",
    "eu": "eu-west-1",
    "apac": "ap-southeast-1",
    "ap": "ap-southeast-1",
}

PT_COLS = ("allocated_mus", "commitment_term", "pt_status")


# --------------------------------------------------------------------------
# Region selection (mirror of the main module's region_for_row)
# --------------------------------------------------------------------------
def region_for_row(model_id, default_region, geo_default_region):
    _, geo = strip_geo_prefix(model_id)
    if geo in (None, "global"):
        return default_region  # can't pin a region for a bare / global id
    return geo_default_region.get(geo, default_region)


# --------------------------------------------------------------------------
# Fetch: provisioned throughputs for one (profile, region)
# --------------------------------------------------------------------------
def get_provisioned_throughputs(bedrock_client, verbose=False):
    """[{model_arn, pt_arn, units, commitment, status}, ...] for this region."""
    out = []
    if bedrock_client is None:
        return out
    try:
        paginator = bedrock_client.get_paginator("list_provisioned_model_throughputs")
        for page in paginator.paginate():
            for pt in page.get("provisionedModelSummaries", []):
                out.append({
                    # field name varies: base-model PTs carry modelArn,
                    # custom-model PTs sometimes only foundationModelArn.
                    "model_arn": pt.get("modelArn", "") or pt.get("foundationModelArn", ""),
                    "pt_arn": pt.get("provisionedModelArn", ""),
                    "units": pt.get("modelUnits", 0),
                    "commitment": pt.get("commitmentDuration") or "None",
                    "status": pt.get("status", "UNKNOWN"),
                })
    except ClientError as e:
        if verbose:
            print(f"  ! Provisioned throughput fetch failed: {e}", file=sys.stderr)
    except NoCredentialsError:
        if verbose:
            print("  ! Provisioned throughput fetch: no credentials", file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# Match: sum MUs across the PTs that belong to this row's base model
# --------------------------------------------------------------------------
def match_provisioned(model_id, pt_list):
    """
    Returns (mus, term, status) or (None, None, None).

    Standalone match is by base-model token inside the PT's model_arn, since the
    report CSV usually carries only the model id, not the foundation-model ARN.
    If you have the real base-model ARN per row, pass a pre-filtered pt_list and
    this still works.
    """
    if not pt_list:
        return None, None, None
    base, _ = strip_geo_prefix(model_id)
    base = (base or "").split(":")[0].lower()  # drop any :version suffix
    if not base:
        return None, None, None
    matches = [p for p in pt_list if base and base in (p["model_arn"] or "").lower()]
    if not matches:
        return None, None, None
    total_mus = sum(p["units"] for p in matches)
    terms = {p["commitment"] for p in matches}
    term = next(iter(terms)) if len(terms) == 1 else "mixed"
    statuses = {p["status"] for p in matches}
    status = "InService" if "InService" in statuses else next(iter(statuses))
    return total_mus, term, status


# --------------------------------------------------------------------------
# Public entry point: add the three PT columns to a dataframe
# --------------------------------------------------------------------------
def enrich_provisioned(df,
                       model_id_col="model_id",
                       account_col="Account",
                       account_to_profile=None,
                       default_region="us-east-1",
                       geo_default_region=None,
                       verbose=False):
    """
    Return a NEW dataframe = df + allocated_mus / commitment_term / pt_status.

    account_to_profile : {account_alias: aws_profile_name}. Rows whose account
                         isn't mapped get blank PT columns. If None, the default
                         credential chain is used for every row.
    Raises KeyError if model_id_col is missing.
    Returns (out_df, stats) where stats = {"rows", "matched", "had_creds"}.
    """
    if model_id_col not in df.columns:
        # Tolerate a bare model-list CSV: try common header spellings, then fall
        # back to the only column if there's exactly one.
        candidates = ["model_id", "modelId", "model", "model_arn", "modelArn", "id"]
        found = next((c for c in candidates if c in df.columns), None)
        if found is None and len(df.columns) == 1:
            found = df.columns[0]
        if found is None:
            raise KeyError(
                f"'{model_id_col}' not in dataframe columns: {list(df.columns)}. "
                f"Pass --model-id-col to name the model column.")
        model_id_col = found

    account_to_profile = account_to_profile or {}
    geo_defaults = {**GEO_DEFAULT_REGION, **(geo_default_region or {})}
    has_account_col = account_col in df.columns

    out = df.copy()

    # --- caches, built once each ---
    session_cache = {}  # profile -> boto3.Session or None
    pt_cache = {}       # (profile, region) -> [pt dicts]

    def session_for(profile):
        if profile not in session_cache:
            try:
                session_cache[profile] = (
                    boto3.Session(profile_name=profile) if profile else boto3.Session()
                )
            except ProfileNotFound:
                session_cache[profile] = None
        return session_cache[profile]

    def provisioned_for(profile, region):
        pk = (profile, region)
        if pk not in pt_cache:
            sess = session_for(profile)
            bc = sess.client("bedrock", region_name=region) if sess else None
            pt_cache[pk] = get_provisioned_throughputs(bc, verbose=verbose)
        return pt_cache[pk]

    cols = {k: [] for k in PT_COLS}
    matched = had_creds_any = 0

    for _, row in out.iterrows():
        model_id = row.get(model_id_col, "")
        region = region_for_row(model_id, default_region, geo_defaults)

        # Credentials: only map by account when both a mapping AND an account
        # column exist. A bare model-list CSV (no Account column) falls through
        # to the default credential chain for every row.
        if account_to_profile and has_account_col:
            account = row.get(account_col, "")
            profile = account_to_profile.get(account)
            have_creds = profile is not None
        else:
            profile, have_creds = None, True  # default chain for all rows

        mus = term = status = None
        if have_creds:
            had_creds_any += 1
            pts = provisioned_for(profile, region)
            mus, term, status = match_provisioned(model_id, pts)
            if mus is not None:
                matched += 1
                if verbose:
                    print(f"  [{model_id}] provisioned: {mus} MU ({term}) {status}",
                          file=sys.stderr)

        cols["allocated_mus"].append(mus)
        cols["commitment_term"].append(term)
        cols["pt_status"].append(status if status is not None else "")

    for col in PT_COLS:
        out[col] = cols[col]

    stats = {"rows": len(out), "matched": matched, "had_creds": had_creds_any}
    return out, stats


# --------------------------------------------------------------------------
# CLI (Jenkins entry point)
# --------------------------------------------------------------------------
def _load_json_arg(value, what):
    """Accept either a path to a JSON file or an inline JSON string."""
    if not value:
        return {}
    if os.path.isfile(value):
        with open(value, "r", encoding="utf-8") as f:
            return json.load(f)
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        print(f"error: --{what} is neither a file nor valid JSON: {e}", file=sys.stderr)
        sys.exit(2)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Add Bedrock Provisioned Throughput (reserved capacity) columns to a report CSV.")
    p.add_argument("-i", "--input", required=True, help="input report CSV")
    p.add_argument("-o", "--output", required=True, help="output CSV to write")
    p.add_argument("--model-id-col", default="model_id")
    p.add_argument("--account-col", default="Account")
    p.add_argument("--account-to-profile", default="",
                   help="path to JSON file or inline JSON: {account_alias: aws_profile}")
    p.add_argument("--default-region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    p.add_argument("--geo-default-region", default="",
                   help='path to JSON file or inline JSON, e.g. {"eu": "eu-central-1"}')
    p.add_argument("--fail-on-empty", action="store_true",
                   help="exit 4 if zero rows matched a provisioned throughput")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2
    try:
        df = pd.read_csv(args.input)
    except Exception as e:
        print(f"error: could not read input CSV: {e}", file=sys.stderr)
        return 2

    account_to_profile = _load_json_arg(args.account_to_profile, "account-to-profile")
    geo_default_region = _load_json_arg(args.geo_default_region, "geo-default-region")

    try:
        out, stats = enrich_provisioned(
            df,
            model_id_col=args.model_id_col,
            account_col=args.account_col,
            account_to_profile=account_to_profile,
            default_region=args.default_region,
            geo_default_region=geo_default_region,
            verbose=args.verbose,
        )
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        out.to_csv(args.output, index=False)
    except Exception as e:
        print(f"error: could not write output CSV: {e}", file=sys.stderr)
        return 2

    print(f"provisioned enrichment: {stats['rows']} rows, "
          f"{stats['had_creds']} with creds, {stats['matched']} matched a PT -> {args.output}",
          file=sys.stderr)

    if stats["had_creds"] == 0:
        print("error: no usable credentials for any row; nothing enriched", file=sys.stderr)
        return 3
    if args.fail_on_empty and stats["matched"] == 0:
        print("error: --fail-on-empty set and zero provisioned matches", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
