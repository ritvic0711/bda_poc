# =============================================================================
# Add to config_schema.py — skeleton-based provisioned Bedrock discovery
# =============================================================================
# model_config.yaml:
#
#   auto_discover:
#     bedrock_provisioned:
#       enabled: true
#       region: "us-east-1"
#       status: "InService"
#       name_contains: null
#       with_pricing: true
#
#       # ONE template stamped onto every discovered model.
#       # AWS supplies name, model_id (provisioned ARN), region, cost, _provisioned.
#       skeleton:
#         streaming: true
#         temperature: 0
#         # max_tokens: 4096
#
#       # optional hand-pins by generated model name (win over everything)
#       overrides:
#         claude_haiku_PT_prod:
#           temperature: 0.2
#           cost: { input_per_1m: 0.90 }
#
# Nothing is written to disk — models are expanded in memory each run.
# For a frozen, inspectable copy use snapshot_discovered_models() (see below).
# =============================================================================

import yaml


def load_models_from_config(config_path: str):
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    models = list(cfg.get("models", []) or [])

    auto = (cfg.get("auto_discover", {}) or {}).get("bedrock_provisioned", {}) or {}
    if auto.get("enabled"):
        from aws_bedrock_discovery import discover_provisioned_models
        discovered = discover_provisioned_models(
            region=auto.get("region", "us-east-1"),
            status=auto.get("status", "InService"),
            name_contains=auto.get("name_contains"),
            with_pricing=auto.get("with_pricing", True),
            skeleton=auto.get("skeleton"),
            overrides=auto.get("overrides"),
        )
        existing = {m.get("name") for m in models}
        for d in discovered:
            if d["name"] not in existing:
                models.append(d)

    models_to_test = {m["name"]: m for m in models}
    cost_matrix = {
        m["name"]: {
            "input_per_1m": m.get("cost", {}).get("input_per_1m", 0.0),
            "output_per_1m": m.get("cost", {}).get("output_per_1m", 0.0),
        }
        for m in models
    }
    test_settings = cfg.get("test_settings", {})
    return models_to_test, cost_matrix, test_settings


# --- OPT-IN: materialize the expanded list to a file (for review / pinning) ---
# from aws_bedrock_discovery import snapshot_discovered_models
# snapshot_discovered_models("model_config.yaml", "models.generated.yaml")
# Then run against the frozen file offline:  --config models.generated.yaml
