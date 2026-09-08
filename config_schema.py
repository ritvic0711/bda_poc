"""
config_schema.py — Fully config-driven model instantiation.

Use exact LangChain param names in the YAML. The code strips framework
metadata and passes everything else to init_chat_model(). That's it.
"""

from typing import Dict, Any
from langchain.chat_models import init_chat_model


# Our framework's metadata — NOT sent to LangChain
_FRAMEWORK_FIELDS = {"name", "provider", "model_type", "cost", "auth_method"}


def _resolve_bedrock_mantle_auth(model_config: Dict[str, Any]) -> Dict[str, str]:
    """Generate IAM bearer token + base_url for Bedrock Mantle models."""
    from datetime import timedelta
    try:
        from aws_bedrock_token_generator import provide_token
    except ImportError:
        raise ImportError(
            "aws_bedrock_token_generator package is required for bedrock_mantle auth. "
            "Install it with: pip install aws-bedrock-token-generator"
        )
    region = model_config.get("region_name", "us-east-1")
    bearer_token = provide_token(region=region, expiry=timedelta(hours=1))
    base_url = f"https://bedrock-mantle.{region}.api.aws/openai/v1"
    return {"api_key": bearer_token, "base_url": base_url}


_AUTH_HANDLERS = {
    "bedrock_mantle_iam": _resolve_bedrock_mantle_auth,
}


def build_model_instance(model_config: Dict[str, Any]):
    """
    Build a LangChain chat model instance from a config entry.

    1. Strip framework metadata
    2. Run auth handler if needed
    3. Pass everything else to init_chat_model()
    """
    params = {k: v for k, v in model_config.items() if k not in _FRAMEWORK_FIELDS}

    auth_method = model_config.get("auth_method")
    if auth_method:
        handler = _AUTH_HANDLERS.get(auth_method)
        if not handler:
            raise ValueError(
                f"Unknown auth_method '{auth_method}' for model "
                f"'{model_config.get('name', '?')}'. "
                f"Supported: {list(_AUTH_HANDLERS.keys())}"
            )
        params.update(handler(model_config))

    if "model_provider" not in params:
        raise ValueError(
            f"Model '{model_config.get('name', '?')}' missing 'model_provider'."
        )

    return init_chat_model(**params)
