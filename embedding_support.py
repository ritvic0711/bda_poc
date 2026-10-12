"""
embedding_support.py — build + test EMBEDDING models (separate from chat).

Embeddings don't use init_chat_model. They use provider Embeddings classes and
produce a vector, not text. So they get their own builder, their own test
(embed a sample -> assert a vector back), and their own metrics (latency, input
tokens, dimension, INPUT-only cost — there is no completion/ttft/throughput).

Config discriminator: mark embedding models with `task: embedding`. Everything
else is treated as chat, exactly as today.
"""
from __future__ import annotations
import os
import time
import logging

logger = logging.getLogger("embeddings")

# framework fields not passed to the Embeddings constructor
_META = {"name", "provider", "model_type", "cost", "auth_method", "task",
         "environment", "profile", "access", "streaming", "temperature",
         "use_responses_api", "store"}


def is_embedding(cfg: dict) -> bool:
    return (cfg.get("task") or "").lower() == "embedding"


def build_embedding_instance(cfg: dict):
    """Build a LangChain Embeddings object from a config entry."""
    provider = (cfg.get("model_provider") or "").lower()
    params = {k: v for k, v in cfg.items() if k not in _META}
    model = params.pop("model", None) or params.pop("model_id", None)

    if provider in ("openai", "azure_openai"):
        # Azure/APIM: same base_url + key + headers as chat, but /embeddings route
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=model, **params)

    if provider == "bedrock_converse" or cfg.get("model_type") == "bedrock_runtime":
        # Bedrock embeddings (Titan / Cohere). model id is the embedding model id.
        from langchain_aws import BedrockEmbeddings
        # BedrockEmbeddings wants region_name + optional creds; drop chat-only keys
        allowed = {"region_name", "aws_access_key_id", "aws_secret_access_key",
                   "aws_session_token", "credentials_profile_name", "base_url"}
        bp = {k: v for k, v in params.items() if k in allowed}
        return BedrockEmbeddings(model_id=model, **bp)

    if provider == "google_genai":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        return GoogleGenerativeAIEmbeddings(model=model, **params)

    raise ValueError(f"no embedding builder for provider {provider!r}")


def _estimate_tokens(text: str) -> int:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text.split()))


def run_embedding_test(embedder, name: str, cfg: dict, sample: str,
                       expected_dim: int | None = None) -> dict:
    """
    Embed one sample. Returns a per-run record with embedding-appropriate metrics
    (chat-only fields left blank so it slots into the same report).
    """
    rates = cfg.get("cost", {}) or {}
    rec = {"model_name": name, "task": "embedding", "status": "", "error_log": "None",
           "total_latency_sec": 0.0, "input_tokens": 0, "embedding_dim": 0,
           "estimated_cost_usd": 0.0,
           # chat-only fields kept for a shared schema, left blank:
           "ttft_sec": "", "completion_tokens": "", "throughput_tok_per_sec": "",
           "json_success_format": ""}
    t0 = time.perf_counter()
    try:
        vec = embedder.embed_query(sample)
        rec["total_latency_sec"] = round(time.perf_counter() - t0, 3)
        dim = len(vec) if vec is not None else 0
        rec["embedding_dim"] = dim
        toks = _estimate_tokens(sample)
        rec["input_tokens"] = toks
        rec["estimated_cost_usd"] = round(toks / 1_000_000 * rates.get("input_per_1m", 0.0), 6)
        ok = dim > 0 and (expected_dim is None or dim == expected_dim)
        rec["status"] = "PASS" if ok else "FAIL"
        if not ok:
            rec["error_log"] = f"bad dim {dim} (expected {expected_dim})"
        logger.info("[embed] %s dim=%d toks=%d %.3fs -> %s", name, dim, toks,
                    rec["total_latency_sec"], rec["status"])
    except Exception as e:
        rec["status"] = "FAIL"
        rec["error_log"] = f"{type(e).__name__}: {e}"
        rec["total_latency_sec"] = round(time.perf_counter() - t0, 3)
        logger.exception("[embed] %s FAIL", name)
    return rec
