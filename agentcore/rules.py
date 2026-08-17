"""Load business_rules.yaml and expose it in the shapes the rest of the POC needs.

This module is the bridge between the ONE human-editable config file and both
memory tiers:
  * STM  -> build_stm_preamble() + rehydrate_turns()
  * LTM  -> consumed by strategies.build_strategies_from_rules()
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
import settings


def load_rules(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else settings.DEFAULT_RULES_PATH
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---- STM ------------------------------------------------------------------

def build_stm_preamble(rules: dict[str, Any]) -> str:
    """The business-rule system preamble injected into the agent every turn.

    STM has no LLM extraction, so 'customizing STM' means controlling the
    system prompt the agent runs with. This is that text.
    """
    return (rules.get("stm", {}).get("system_preamble") or "").strip()


def rehydrate_turns(rules: dict[str, Any]) -> int:
    return int(rules.get("stm", {}).get("rehydrate_turns", 5))


def retention_days(rules: dict[str, Any]) -> int:
    return int(rules.get("memory", {}).get("retention_days", 30))


# ---- shared ---------------------------------------------------------------

def region(rules: dict[str, Any]) -> str:
    return rules["memory"]["region"]


def memory_name(rules: dict[str, Any]) -> str:
    return rules["memory"]["name"]


def ltm_model_id(rules: dict[str, Any]) -> str:
    return rules["memory"]["ltm_model_id"]


def uses_overrides(rules: dict[str, Any]) -> bool:
    """True if any enabled LTM strategy carries an append_to_prompt override."""
    for cfg in rules.get("ltm", {}).values():
        if not cfg.get("enabled"):
            continue
        for step in ("extraction", "consolidation"):
            if (cfg.get(step) or {}).get("append_to_prompt"):
                return True
    return False


def resolve_namespace(template: str, actor_id: str, session_id: str = "") -> str:
    """Fill {actorId}/{sessionId} placeholders so we can query LTM records."""
    return template.replace("{actorId}", actor_id).replace("{sessionId}", session_id)
