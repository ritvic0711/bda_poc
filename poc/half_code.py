ENRICH_COLS = (
    "lifecycle_status", "is_legacy", "eol_date",
    "inference_type", "allocated_rpm", "allocated_tpm",
    "allocated_mus", "commitment_term", "pt_status")



def get_provisioned_throughputs(bedrock_client, verbose=False):
    """[{model_arn, pt_arn, units, commitment, status}, ...] for this (profile, region)."""
    out = []
    try:
        for page in bedrock_client.get_paginator(
                "list_provisioned_model_throughputs").paginate():
            for pt in page.get("provisionedModelSummaries", []):
                out.append({
                    "model_arn": pt.get("modelArn", "") or pt.get("foundationModelArn", ""),
                    "pt_arn": pt.get("provisionedModelArn", ""),
                    "units": pt.get("modelUnits", 0),
                    "commitment": pt.get("commitmentDuration") or "None",
                    "status": pt.get("status", "UNKNOWN"),
                })
    except ClientError as e:
        if verbose:
            print(f"  ! Provisioned throughput fetch failed: {e}", file=sys.stderr)
    return out

def match_provisioned(model_id, fm, pt_list):
    """Sum model units across PTs for this row's base model. (mus, term, status) or (None, None, None)."""
    base_arn = (fm or {}).get("modelArn", "") if fm else ""
    matches = [p for p in pt_list if base_arn and p["model_arn"] == base_arn]
    if not matches:
        base, _ = strip_geo_prefix(model_id)
        base = base.split(":")[0].lower()
        if base:
            matches = [p for p in pt_list if base in p["model_arn"].lower()]
    if not matches:
        return None, None, None
    total_mus = sum(p["units"] for p in matches)
    terms = {p["commitment"] for p in matches}
    term = next(iter(terms)) if len(terms) == 1 else "mixed"
    statuses = {p["status"] for p in matches}
    status = "InService" if "InService" in statuses else next(iter(statuses))
    return total_mus, term, status


pt_cache = {}  # (profile, region) -> [pt dicts]

def bedrock_client_for(profile, region):
    sess = session_for(profile)
    return sess.client("bedrock", region_name=region) if sess else None

def provisioned_for(profile, region):
    pk = (profile, region)
    if pk not in pt_cache:
        bc = bedrock_client_for(profile, region)
        pt_cache[pk] = get_provisioned_throughputs(bc, verbose=verbose) if bc else []
    return pt_cache[pk]


allocated_mus = commitment_term = pt_status = None

pts = provisioned_for(profile, region)
allocated_mus, commitment_term, pt_status = match_provisioned(model_id, fm, pts)
if allocated_mus is not None:
    notes.append(f"provisioned: {allocated_mus} MU ({commitment_term})")


cols["allocated_mus"].append(allocated_mus)
cols["commitment_term"].append(commitment_term)
cols["pt_status"].append(pt_status)