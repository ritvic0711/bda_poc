"""
aws_bedrock_discovery.py — Discover your account's PROVISIONED THROUGHPUT
Bedrock models and attach reference per-1M-token prices.

discover_provisioned_models() -> [ {name, provider, model_type, model_id,
                                    model_provider, region, streaming,
                                    temperature, cost:{input_per_1m, output_per_1m},
                                    _provisioned:{...raw...}}, ... ]

Two things to be clear about:

  1. This lists ONLY provisioned-throughput commitments in your account
     (ListProvisionedModelThroughputs). On-demand / inference-profile models do
     NOT appear here.

  2. Provisioned throughput is billed HOURLY per model unit, on a 1- or 6-month
     commitment — NOT per token. The input_per_1m / output_per_1m attached here
     are the ON-DEMAND list-price equivalent of the UNDERLYING foundation model
     (from the AWS Price List API), i.e. a reference/comparison rate for your
     cost formula. It is not what provisioned capacity actually bills you.
     For true provisioned pricing you must contact your AWS account team.

Requires boto3 and IAM perms:
  bedrock:ListProvisionedModelThroughputs, pricing:GetProducts
"""

import json
import re
import logging
from typing import Dict, Any, List, Optional

import boto3

logger = logging.getLogger("bedrock_discovery")

# Defaults applied to every discovered model when the config skeleton omits them.
DEFAULT_SKELETON = {
    "provider": "aws",
    "model_type": "bedrock_runtime",
    "model_provider": "bedrock_converse",
    "streaming": True,
    "temperature": 0,
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay onto base (overlay wins on scalars)."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

_PRICING_ENDPOINT_REGION = "us-east-1"   # Price List API lives here (or ap-south-1)

_REGION_TO_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
    "eu-central-1": "EU (Frankfurt)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
}


# ---------------------------------------------------------------------------
# Reference pricing (on-demand equivalent of the underlying foundation model)
# ---------------------------------------------------------------------------
class BedrockPricing:
    def __init__(self, region: str = "us-east-1", session: Optional[boto3.Session] = None):
        self.region = region
        session = session or boto3.Session()
        self._pricing = session.client("pricing", region_name=_PRICING_ENDPOINT_REGION)
        self._cache: Dict[str, Dict[str, float]] = {}
        self._loaded_regions: set = set()

    @staticmethod
    def normalize_model_id(model_id: str) -> str:
        mid = model_id.split("/")[-1]                              # drop ARN path
        mid = re.sub(r"^(global|us|eu|apac|us-gov)\.", "", mid)    # drop region prefix
        mid = re.sub(r"-v\d+:\d+$", "", mid)                       # drop -v1:0
        mid = re.sub(r":\d+.*$", "", mid)                          # drop :24k etc.
        mid = re.sub(r"-\d{8}$", "", mid)                          # drop -20251001
        return mid.lower()

    def _to_per_1m(self, price_per_unit: float, unit: str) -> float:
        u = (unit or "").lower()
        if "1000000" in u or "1m" in u:
            return price_per_unit
        if "1000" in u or "1k" in u or "1,000" in u:
            return price_per_unit * 1000.0
        return price_per_unit * 1000.0   # token usage types are most often per-1K

    def _load_region(self, region: str) -> None:
        if region in self._loaded_regions:
            return
        filters = [
            {"Type": "TERM_MATCH", "Field": "serviceCode", "Value": "AmazonBedrock"},
            {"Type": "TERM_MATCH", "Field": "feature", "Value": "On-demand Inference"},
        ]
        location = _REGION_TO_LOCATION.get(region)
        filters.append(
            {"Type": "TERM_MATCH", "Field": "location", "Value": location} if location
            else {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}
        )
        paginator = self._pricing.get_paginator("get_products")
        for page in paginator.paginate(ServiceCode="AmazonBedrock", Filters=filters):
            for raw in page.get("PriceList", []):
                self._ingest(json.loads(raw))
        self._loaded_regions.add(region)

    def _ingest(self, item: Dict[str, Any]) -> None:
        attrs = item.get("product", {}).get("attributes", {})
        model = attrs.get("model") or attrs.get("titanModel") or attrs.get("modelName")
        if not model:
            return
        usagetype = (attrs.get("usagetype", "") + " " + attrs.get("operation", "")).lower()
        if "input" in usagetype:
            direction = "input_per_1m"
        elif "output" in usagetype:
            direction = "output_per_1m"
        else:
            return
        for term in item.get("terms", {}).get("OnDemand", {}).values():
            for dim in term.get("priceDimensions", {}).values():
                usd = dim.get("pricePerUnit", {}).get("USD")
                if usd is None:
                    continue
                per_1m = self._to_per_1m(float(usd), dim.get("unit", ""))
                key = self.normalize_model_id(model)
                self._cache.setdefault(key, {})[direction] = round(per_1m, 6)

    def get_rates(self, model_id: str, region: str) -> Dict[str, float]:
        try:
            self._load_region(region)
        except Exception as e:
            logger.warning("Price List API failed for %s: %s", region, e)
            return {}
        return self._cache.get(self.normalize_model_id(model_id), {})


# ---------------------------------------------------------------------------
# Provisioned model discovery
# ---------------------------------------------------------------------------
def _region_from_arn(arn: str, fallback: str) -> str:
    # arn:aws:bedrock:us-east-1:acct:provisioned-model/xxxx
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 and parts[3] else fallback


def discover_provisioned_models(
    region: str = "us-east-1",
    status: str = "InService",           # only ready-to-use commitments
    name_contains: Optional[str] = None,
    with_pricing: bool = True,
    skeleton: Optional[Dict[str, Any]] = None,     # shared defaults for every model
    overrides: Optional[Dict[str, Any]] = None,    # per-name hand pins, win over all
    session: Optional[boto3.Session] = None,
) -> List[Dict[str, Any]]:
    """
    List provisioned-throughput models in the account, shaped as config entries.

    IMPORTANT: model_id is the PROVISIONED ARN (that's what you invoke against),
    while pricing is looked up from the UNDERLYING foundationModelArn.
    """
    session = session or boto3.Session()
    bedrock_client = session.client("bedrock", region_name=region)
    pricing = BedrockPricing(region=region, session=session) if with_pricing else None

    summaries: List[Dict[str, Any]] = []
    token = None
    while True:
        kwargs: Dict[str, Any] = {}
        if status:
            kwargs["statusEquals"] = status
        if name_contains:
            kwargs["nameContains"] = name_contains
        if token:
            kwargs["nextToken"] = token
        resp = bedrock_client.list_provisioned_model_throughputs(**kwargs)
        summaries.extend(resp.get("provisionedModelSummaries", []))
        token = resp.get("nextToken")
        if not token:
            break

    if not summaries:
        logger.warning(
            "No provisioned-throughput models found in %s (status=%s). "
            "Note: on-demand / inference-profile models never appear here.",
            region, status,
        )

    base_skeleton = _deep_merge(DEFAULT_SKELETON, skeleton or {})
    overrides = overrides or {}

    models: List[Dict[str, Any]] = []
    for s in summaries:
        invoke_arn = s.get("provisionedModelArn", "")          # invoke on this
        underlying = s.get("foundationModelArn") or s.get("modelArn", "")  # price on this
        pm_region = _region_from_arn(invoke_arn, region)
        name = _safe_name(s.get("provisionedModelName"), invoke_arn)

        # model-specific facts from AWS (these win over skeleton scaffolding)
        facts: Dict[str, Any] = {
            "name": name,
            "model_id": invoke_arn,                             # the provisioned ARN
            "region": pm_region,
            "_provisioned": {
                "underlying_model_arn": underlying,
                "model_units": s.get("modelUnits"),
                "commitment_duration": s.get("commitmentDuration"),
                "commitment_expiration": str(s.get("commitmentExpirationTime", "")),
                "status": s.get("status"),
            },
        }
        if pricing:
            rates = pricing.get_rates(underlying, pm_region)
            facts["cost"] = {
                "input_per_1m": rates.get("input_per_1m", 0.0),
                "output_per_1m": rates.get("output_per_1m", 0.0),
                "_note": "on-demand-equivalent list price of underlying model; "
                         "provisioned capacity bills hourly, not per token",
            }
            if not rates:
                logger.warning(
                    "No list price matched underlying model %s — cost defaulted to 0.0",
                    underlying,
                )

        # skeleton (shared) <- AWS facts (model-specific) <- overrides (hand pins)
        entry = _deep_merge(base_skeleton, facts)
        entry = _deep_merge(entry, overrides.get(name, {}))
        models.append(entry)

    return models


def snapshot_discovered_models(config_path: str, out_path: str = "models.generated.yaml") -> str:
    """
    OPT-IN write-to-file. Expand the skeleton against live AWS discovery and write
    the fully materialized model list to `out_path` for inspection / pinning /
    committing a known-good set. Does NOT touch your source config.
    Returns the path written.
    """
    import yaml
    if out_path == config_path:
        raise ValueError("Refusing to overwrite the source config; choose a different out_path.")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    auto = (cfg.get("auto_discover", {}) or {}).get("bedrock_provisioned", {}) or {}
    discovered = discover_provisioned_models(
        region=auto.get("region", "us-east-1"),
        status=auto.get("status", "InService"),
        name_contains=auto.get("name_contains"),
        with_pricing=auto.get("with_pricing", True),
        skeleton=auto.get("skeleton"),
        overrides=auto.get("overrides"),
    )
    header = ("# GENERATED SNAPSHOT — do not hand-edit; regenerate with "
              "snapshot_discovered_models().\n# Provisioned ARNs and prices are "
              "point-in-time.\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"models": discovered}, f, sort_keys=False, default_flow_style=False)
    logger.info("Wrote %d models to %s", len(discovered), out_path)
    return out_path


def _safe_name(label: Optional[str], fallback: str) -> str:
    base = label or fallback.split("/")[-1] or fallback
    return re.sub(r"[^0-9a-zA-Z]+", "_", base).strip("_")
