"""Environment + path settings for the POC.

Nothing secret is hardcoded. Region and model come from business_rules.yaml;
the memory execution role ARN comes from the environment (only needed when a
strategy is overridden).
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH ="business_rules.yaml"

# Where the resolved memory_id is cached between runs so we reuse (not recreate)
# the memory resource. Delete this file to force a fresh create.
MEMORY_ID_CACHE = REPO_ROOT / ".memory_id"


def execution_role_arn(env_var: str) -> str | None:
    """Read the memory execution role ARN from the environment.

    Required only when at least one LTM strategy is overridden, because in that
    case AgentCore invokes Bedrock in *your* account and needs a role to assume.
    """
    return os.environ.get(env_var)
