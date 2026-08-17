"""Every AgentCore Memory long-term strategy, built as create_memory() dicts.

AgentCore gives you FOUR ways to populate long-term memory. The first three are
service-managed built-ins (no LLM cost to your account, no prompt to maintain).
The fourth ("built-in with overrides" / customMemoryStrategy) lets you inject
your own instructions and pin the model — this is the one the POC is about.

    1. semanticMemoryStrategy        -> extracts durable facts
    2. userPreferenceMemoryStrategy  -> extracts user preferences
    3. summaryMemoryStrategy         -> per-session rolling summary
    4. customMemoryStrategy          -> any of the above, but with your
                                        extraction/consolidation prompt +
                                        model overridden

Override sub-keys map to the base type:
    semanticOverride        -> extraction + consolidation
    userPreferenceOverride  -> extraction + consolidation
    summaryOverride         -> consolidation only (summary has no extraction step)
"""
from __future__ import annotations

from typing import Any

# Human-readable catalog printed by the demo so you can SEE all strategies at
# once and how they differ. (name, tier, has_extraction, has_consolidation, note)
STRATEGY_CATALOG = [
    ("semanticMemoryStrategy", "built-in", True, True,
     "Durable facts. Managed extraction+consolidation. No prompt, no acct LLM cost."),
    ("userPreferenceMemoryStrategy", "built-in", True, True,
     "User preferences/choices/styles. Managed. Good for personalization."),
    ("summaryMemoryStrategy", "built-in", False, True,
     "Rolling per-session summary. Managed. Consolidation only."),
    ("customMemoryStrategy (semanticOverride)", "built-in+override", True, True,
     "Semantic facts, YOUR extraction+consolidation prompt + model. Auditable."),
    ("customMemoryStrategy (userPreferenceOverride)", "built-in+override", True, True,
     "Preferences with YOUR prompts + model."),
    ("customMemoryStrategy (summaryOverride)", "built-in+override", False, True,
     "Summary shaped by YOUR consolidation prompt + model."),
]


# ---- plain built-ins (no overrides) --------------------------------------

def semantic_builtin(name: str, namespace: str) -> dict[str, Any]:
    return {"semanticMemoryStrategy": {"name": name, "namespaceTemplates": [namespace]}}


def user_preference_builtin(name: str, namespace: str) -> dict[str, Any]:
    return {"userPreferenceMemoryStrategy": {"name": name, "namespaceTemplates": [namespace]}}


def summary_builtin(name: str, namespace: str) -> dict[str, Any]:
    return {"summaryMemoryStrategy": {"name": name, "namespaceTemplates": [namespace]}}


# ---- built-in with overrides (customMemoryStrategy) ----------------------

def _step(append_to_prompt: str | None, model_id: str) -> dict[str, Any] | None:
    if not append_to_prompt:
        return None
    return {"appendToPrompt": append_to_prompt, "modelId": model_id}


def semantic_override(name: str, namespace: str, model_id: str,
                      extraction_prompt: str | None,
                      consolidation_prompt: str | None) -> dict[str, Any]:
    override: dict[str, Any] = {}
    if (ext := _step(extraction_prompt, model_id)):
        override["extraction"] = ext
    if (con := _step(consolidation_prompt, model_id)):
        override["consolidation"] = con
    return {
        "customMemoryStrategy": {
            "name": name,
            "namespaceTemplates": [namespace],
            "configuration": {"semanticOverride": override},
        }
    }


def user_preference_override(name: str, namespace: str, model_id: str,
                             extraction_prompt: str | None,
                             consolidation_prompt: str | None) -> dict[str, Any]:
    override: dict[str, Any] = {}
    if (ext := _step(extraction_prompt, model_id)):
        override["extraction"] = ext
    if (con := _step(consolidation_prompt, model_id)):
        override["consolidation"] = con
    return {
        "customMemoryStrategy": {
            "name": name,
            "namespaceTemplates": [namespace],
            "configuration": {"userPreferenceOverride": override},
        }
    }


def summary_override(name: str, namespace: str, model_id: str,
                     consolidation_prompt: str | None) -> dict[str, Any]:
    # summary only supports a consolidation override
    override = {"consolidation": _step(consolidation_prompt, model_id)}
    return {
        "customMemoryStrategy": {
            "name": name,
            "namespaceTemplates": [namespace],
            "configuration": {"summaryOverride": override},
        }
    }


# ---- assemble from business_rules.yaml -----------------------------------

def build_strategies_from_rules(rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the ltm section of business_rules.yaml into a create_memory() list.

    For each enabled strategy: if it carries an append_to_prompt, emit the
    override (custom) form; otherwise emit the plain built-in.
    """
    model_id = rules["memory"]["ltm_model_id"]
    ltm = rules.get("ltm", {})
    out: list[dict[str, Any]] = []

    def prompt(cfg: dict, step: str) -> str | None:
        return (cfg.get(step) or {}).get("append_to_prompt")

    if (s := ltm.get("semantic", {})).get("enabled"):
        ns, name = s["namespace"], "semantic_facts"
        ext, con = prompt(s, "extraction"), prompt(s, "consolidation")
        out.append(semantic_override(name, ns, model_id, ext, con)
                   if (ext or con) else semantic_builtin(name, ns))

    if (p := ltm.get("user_preference", {})).get("enabled"):
        ns, name = p["namespace"], "user_preferences"
        ext, con = prompt(p, "extraction"), prompt(p, "consolidation")
        out.append(user_preference_override(name, ns, model_id, ext, con)
                   if (ext or con) else user_preference_builtin(name, ns))

    if (sm := ltm.get("summary", {})).get("enabled"):
        ns, name = sm["namespace"], "session_summary"
        con = prompt(sm, "consolidation")
        out.append(summary_override(name, ns, model_id, con)
                   if con else summary_builtin(name, ns))

    return out
