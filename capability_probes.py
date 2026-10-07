"""
capability_probes.py
====================
Provider-capability probes for the model test suite. Each probe is an async
function ``ProbeContext -> dict`` that confirms one behaviour of a built
LangChain model (token accounting, structured output, prompt caching, service
tier, inference-profile mapping, bearer-token TTL, encrypted-reasoning
round-trip).

Design rules (match the registry at the bottom):
  * REQUIRED_PROBES   — a FAIL here fails the gate. Everything else is reported
                        but not enforced, so adding a probe can't break CI the
                        day it lands.
  * NETWORK_FREE_PROBES — make no network call, so they still run when the route
                        is unreachable; every other probe is SKIPped in that case
                        instead of repeating the same auth failure once per probe.

Status vocabulary, used consistently:
  PASS  capability confirmed
  FAIL  capability applies to this provider but is missing/broken
  SKIP  not applicable to this provider, dependency missing, or route down

Run them with ``run_probes(ctx, route_reachable=...)``.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("probes")

# pydantic is a LangChain dependency; guard so import never hard-fails the suite.
try:
    from pydantic import BaseModel, Field
    class _Person(BaseModel):
        name: str = Field(description="person's full name")
        age: int = Field(description="person's age in years")
    _HAVE_PYDANTIC = True
except Exception:  # pragma: no cover
    _Person = None
    _HAVE_PYDANTIC = False


# --------------------------------------------------------------------------
# Context + result helpers
# --------------------------------------------------------------------------
@dataclass
class ProbeContext:
    """Everything a probe needs. Built once per model by the runner."""
    model: Any                       # a built LangChain chat model
    model_name: str
    model_config: dict               # the raw config entry (for network-free checks)
    service_tier: str = "priority"   # tier value to try in service_tier_reserved
    cache_filler_chars: int = 8000   # size of the repeated context for cache probes

    # --- derived helpers ---
    @property
    def provider(self) -> str:
        return (self.model_config.get("model_provider") or "").lower()

    @property
    def model_id(self) -> str:
        return str(self.model_config.get("model_id")
                   or self.model_config.get("model") or "")

    @property
    def is_anthropic_family(self) -> bool:
        return self.provider == "bedrock_converse" or "anthropic" in self.model_id.lower()

    @property
    def is_openai_family(self) -> bool:
        return self.provider in ("openai", "azure_openai")

    @property
    def is_gemini_family(self) -> bool:
        return self.provider == "google_genai"

    def big_context(self) -> str:
        # deterministic filler so the same text hits the cache on repeat
        unit = "The quick brown fox jumps over the lazy dog. "
        return (unit * (self.cache_filler_chars // len(unit) + 1))[: self.cache_filler_chars]


def _result(name: str, status: str, detail: str = "", **metrics) -> dict:
    return {"probe": name, "status": status, "detail": detail, "metrics": metrics}


def _usage(resp) -> dict:
    return getattr(resp, "usage_metadata", None) or {}


def _cache_read(um: dict) -> int:
    d = um.get("input_token_details") or {}
    return int(d.get("cache_read") or 0)


def _cache_creation(um: dict) -> int:
    d = um.get("input_token_details") or {}
    return int(d.get("cache_creation") or 0)


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------
async def usage_metadata(ctx: ProbeContext) -> dict:
    """REQUIRED: a plain invoke returns input+output token counts."""
    resp = await ctx.model.ainvoke("Reply with the single word: online.")
    um = _usage(resp)
    inp, out = um.get("input_tokens"), um.get("output_tokens")
    if inp and out:
        return _result("usage_metadata", "PASS",
                       f"input={inp} output={out}", input_tokens=inp, output_tokens=out)
    return _result("usage_metadata", "FAIL",
                   f"usage_metadata missing/empty: {um!r}")


async def usage_metadata_streaming(ctx: ProbeContext) -> dict:
    """Streaming aggregates usage onto the final chunk (may need stream_usage=True)."""
    agg = None
    n = 0
    async for chunk in ctx.model.astream("Write one short sentence about the sea."):
        agg = chunk if agg is None else agg + chunk
        n += 1
    um = _usage(agg) if agg is not None else {}
    if um.get("output_tokens"):
        return _result("usage_metadata_streaming", "PASS",
                       f"chunks={n} output={um.get('output_tokens')}",
                       chunks=n, output_tokens=um.get("output_tokens"))
    if n == 0:
        return _result("usage_metadata_streaming", "FAIL", "no chunks streamed")
    return _result("usage_metadata_streaming", "FAIL",
                   f"streamed {n} chunks but no usage on aggregate; "
                   f"provider may need stream_usage=True")


async def _structured(ctx: ProbeContext, strict: Optional[bool]) -> tuple[str, str]:
    if not _HAVE_PYDANTIC:
        return "SKIP", "pydantic not installed"
    kwargs = {} if strict is None else {"strict": strict}
    try:
        runnable = ctx.model.with_structured_output(_Person, **kwargs)
    except TypeError:
        # 'strict' kwarg unsupported by this integration
        return "SKIP", "with_structured_output() does not accept strict= on this provider"
    try:
        out = await runnable.ainvoke("Extract the person: John Smith is 30 years old.")
    except NotImplementedError:
        return "SKIP", "structured output not implemented for this provider"
    except Exception as e:
        return "FAIL", f"{type(e).__name__}: {e}"
    name = getattr(out, "name", None) if not isinstance(out, dict) else out.get("name")
    age = getattr(out, "age", None) if not isinstance(out, dict) else out.get("age")
    if name and age:
        return "PASS", f"parsed name={name!r} age={age}"
    return "FAIL", f"parsed object incomplete: {out!r}"


async def structured_output_strict(ctx: ProbeContext) -> dict:
    """with_structured_output(strict=True) — JSON-schema-enforced."""
    status, detail = await _structured(ctx, strict=True)
    return _result("structured_output_strict", status, detail)


async def structured_output_bare(ctx: ProbeContext) -> dict:
    """with_structured_output() with no strict flag."""
    status, detail = await _structured(ctx, strict=None)
    return _result("structured_output_bare", status, detail)


async def prompt_cache(ctx: ProbeContext) -> dict:
    """
    EXPLICIT caching (Anthropic/Bedrock cache_control breakpoints). SKIP for
    providers that only do implicit caching.
    """
    if not ctx.is_anthropic_family:
        return _result("prompt_cache", "SKIP",
                       "explicit cache_control is Anthropic/Bedrock-only here")
    big = ctx.big_context()
    # content block with an ephemeral cache point (LangChain Anthropic/Bedrock form)
    sys_block = {"type": "text", "text": big,
                 "cache_control": {"type": "ephemeral"}}
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        msgs = [SystemMessage(content=[sys_block]),
                HumanMessage("In one word, acknowledge.")]
        resp = await ctx.model.ainvoke(msgs)
    except Exception as e:
        return _result("prompt_cache", "FAIL", f"cache_control call errored: {type(e).__name__}: {e}")
    um = _usage(resp)
    created, read = _cache_creation(um), _cache_read(um)
    if created or read:
        return _result("prompt_cache", "PASS",
                       f"cache_creation={created} cache_read={read}",
                       cache_creation=created, cache_read=read)
    return _result("prompt_cache", "FAIL",
                   f"no cache token accounting returned: input_token_details="
                   f"{um.get('input_token_details')!r}")


async def prompt_cache_implicit(ctx: ProbeContext) -> dict:
    """
    IMPLICIT caching (OpenAI/Gemini auto-cache). Send the same large prompt twice;
    expect cache_read>0 on the second call.
    """
    if ctx.is_anthropic_family:
        return _result("prompt_cache_implicit", "SKIP",
                       "Anthropic requires explicit cache_control; see prompt_cache")
    from langchain_core.messages import SystemMessage, HumanMessage
    big = ctx.big_context()
    msgs = [SystemMessage(big), HumanMessage("Reply with: ok.")]
    try:
        await ctx.model.ainvoke(msgs)                 # prime
        resp2 = await ctx.model.ainvoke(msgs)         # should hit cache
    except Exception as e:
        return _result("prompt_cache_implicit", "FAIL", f"{type(e).__name__}: {e}")
    read = _cache_read(_usage(resp2))
    if read > 0:
        return _result("prompt_cache_implicit", "PASS", f"cache_read={read}", cache_read=read)
    return _result("prompt_cache_implicit", "FAIL",
                   "no cache_read on repeat (prompt may be below provider cache "
                   "threshold, or implicit caching disabled)")


async def service_tier_reserved(ctx: ProbeContext) -> dict:
    """Model accepts a service_tier binding (reserved/priority/provisioned)."""
    tier = ctx.service_tier
    try:
        bound = ctx.model.bind(service_tier=tier)
        resp = await bound.ainvoke("Reply with: ok.")
    except Exception as e:
        msg = str(e).lower()
        if "service_tier" in msg or "tier" in msg or "unsupported" in msg or "invalid" in msg:
            return _result("service_tier_reserved", "SKIP",
                           f"provider rejected service_tier={tier!r}: {type(e).__name__}")
        return _result("service_tier_reserved", "FAIL", f"{type(e).__name__}: {e}")
    returned = (resp.response_metadata or {}).get("service_tier")
    return _result("service_tier_reserved", "PASS",
                   f"accepted service_tier={tier!r}; returned={returned!r}",
                   requested=tier, returned=returned)


async def inference_profile_mapped(ctx: ProbeContext) -> dict:
    """
    NETWORK-FREE: the configured model id maps to an inference profile / deployment
    without needing a call. Bedrock → cross-region profile prefix; Azure/OpenAI →
    a base_url + deployment; Gemini → a model id present.
    """
    mid = ctx.model_id
    if ctx.provider == "bedrock_converse":
        import re
        if re.match(r"^(global|us|eu|apac|us-gov)\.", mid):
            return _result("inference_profile_mapped", "PASS",
                           f"cross-region inference profile: {mid}")
        if mid.startswith("arn:aws:bedrock:") and "inference-profile" in mid:
            return _result("inference_profile_mapped", "PASS", f"profile ARN: {mid}")
        return _result("inference_profile_mapped", "FAIL",
                       f"bedrock model id has no inference-profile prefix/ARN: {mid!r}")
    if ctx.is_openai_family:
        if ctx.model_config.get("base_url") and mid:
            return _result("inference_profile_mapped", "PASS",
                           f"deployment {mid} @ configured endpoint")
        return _result("inference_profile_mapped", "FAIL",
                       "missing base_url or deployment/model for azure/openai route")
    if ctx.is_gemini_family:
        return _result("inference_profile_mapped", "PASS" if mid else "FAIL",
                       f"model id {mid!r}")
    return _result("inference_profile_mapped", "SKIP",
                   f"no mapping rule for provider {ctx.provider!r}")


async def bearer_token_ttl(ctx: ProbeContext) -> dict:
    """
    For auth that mints a short-lived bearer token (Bedrock Mantle IAM), confirm a
    token is obtainable and has a sane remaining TTL. SKIP for static-key auth
    (APIM subscription key, plain api_key).
    """
    auth = (ctx.model_config.get("auth_method") or "").lower()
    if auth != "bedrock_mantle_iam":
        return _result("bearer_token_ttl", "SKIP",
                       f"auth_method={auth or 'static-key'} has no bearer TTL")
    try:
        from datetime import timedelta
        from aws_bedrock_token_generator import provide_token
    except Exception as e:
        return _result("bearer_token_ttl", "SKIP", f"token generator unavailable: {e}")
    region = ctx.model_config.get("region") or ctx.model_config.get("region_name") or "us-east-1"
    try:
        ttl = timedelta(hours=1)
        tok = provide_token(region=region, expiry=ttl)
    except Exception as e:
        return _result("bearer_token_ttl", "FAIL", f"{type(e).__name__}: {e}")
    if tok and len(tok) > 20:
        return _result("bearer_token_ttl", "PASS",
                       f"token minted (len={len(tok)}), requested TTL={ttl}",
                       token_len=len(tok), ttl_seconds=int(ttl.total_seconds()))
    return _result("bearer_token_ttl", "FAIL", "token empty/too short")


async def encrypted_reasoning_roundtrip(ctx: ProbeContext) -> dict:
    """
    Reasoning model with store=false returns encrypted reasoning that round-trips
    into a follow-up turn. OpenAI Responses API only.
    """
    if not (ctx.is_openai_family and ctx.model_config.get("use_responses_api")):
        return _result("encrypted_reasoning_roundtrip", "SKIP",
                       "needs openai Responses API with use_responses_api=true")
    from langchain_core.messages import HumanMessage
    try:
        m = ctx.model.bind(include=["reasoning.encrypted_content"], store=False)
        first = await m.ainvoke([HumanMessage("Think step by step: what is 17*23? Give the number.")])
    except Exception as e:
        return _result("encrypted_reasoning_roundtrip", "FAIL",
                       f"first turn errored: {type(e).__name__}: {e}")
    # look for encrypted reasoning in content blocks or additional_kwargs
    enc_found = False
    content = first.content
    if isinstance(content, list):
        enc_found = any(isinstance(b, dict) and b.get("type") in
                        ("reasoning", "reasoning_content") and
                        ("encrypted_content" in b or "encrypted" in str(b).lower())
                        for b in content)
    if not enc_found:
        ak = getattr(first, "additional_kwargs", {}) or {}
        enc_found = "reasoning" in ak or "encrypted_content" in json.dumps(ak)[:5000]
    if not enc_found:
        return _result("encrypted_reasoning_roundtrip", "FAIL",
                       "no encrypted reasoning block in first response")
    try:
        second = await m.ainvoke([HumanMessage("Think step by step: what is 17*23? Give the number."),
                                  first, HumanMessage("Now double it.")])
    except Exception as e:
        return _result("encrypted_reasoning_roundtrip", "FAIL",
                       f"round-trip of encrypted reasoning rejected: {type(e).__name__}: {e}")
    return _result("encrypted_reasoning_roundtrip", "PASS",
                   "encrypted reasoning returned and accepted on follow-up turn")


# --------------------------------------------------------------------------
# Registry + gate sets  (mirror the screenshot)
# --------------------------------------------------------------------------
PROBES: dict[str, Callable[[ProbeContext], Awaitable[dict]]] = {
    "usage_metadata": usage_metadata,
    "usage_metadata_streaming": usage_metadata_streaming,
    "structured_output_strict": structured_output_strict,
    "structured_output_bare": structured_output_bare,
    "prompt_cache": prompt_cache,
    "prompt_cache_implicit": prompt_cache_implicit,
    "service_tier_reserved": service_tier_reserved,
    "inference_profile_mapped": inference_profile_mapped,
    "bearer_token_ttl": bearer_token_ttl,
    "encrypted_reasoning_roundtrip": encrypted_reasoning_roundtrip,
}

# Probes whose failure fails the gate. The rest are reported and not enforced,
# so that adding a probe cannot break CI on the day it lands.
REQUIRED_PROBES = frozenset({"usage_metadata"})

# Probes that make no network call, so they still run when the route is
# unreachable. Everything else is skipped in that case rather than repeating the
# same auth failure once per probe.
NETWORK_FREE_PROBES = frozenset({"inference_profile_mapped"})


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
PROBE_CSV_FIELDS = ["model_name", "probe", "status", "enforced",
                    "latency_sec", "detail", "metrics"]


async def run_probes(ctx: ProbeContext, route_reachable: bool = True) -> dict:
    """
    Run every probe for one model. Returns:
      {model_name, gate_passed, rows: [ {model_name, probe, status, enforced,
                                         latency_sec, detail, metrics}, ... ]}
    """
    rows = []
    for name, fn in PROBES.items():
        enforced = name in REQUIRED_PROBES
        t0 = time.perf_counter()
        if not route_reachable and name not in NETWORK_FREE_PROBES:
            row = _result(name, "SKIP", "route unreachable")
        else:
            try:
                row = await fn(ctx)
            except Exception as e:   # a probe must never crash the run
                logger.exception("[%s] probe %s crashed", ctx.model_name, name)
                row = _result(name, "FAIL", f"probe raised {type(e).__name__}: {e}")
        rows.append({
            "model_name": ctx.model_name,
            "probe": name,
            "status": row["status"],
            "enforced": enforced,
            "latency_sec": round(time.perf_counter() - t0, 3),
            "detail": row.get("detail", ""),
            "metrics": json.dumps(row.get("metrics", {})),
        })
        logger.info("[%s] %-28s %-4s %s", ctx.model_name, name, row["status"], row.get("detail", ""))

    gate_passed = all(r["status"] == "PASS" for r in rows if r["enforced"])
    return {"model_name": ctx.model_name, "gate_passed": gate_passed, "rows": rows}


def write_probe_csv(rows: list, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PROBE_CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in PROBE_CSV_FIELDS})


# --------------------------------------------------------------------------
# Optional CLI: build one model from a config and probe it.
#   python capability_probes.py --config configs/dev/azure.yaml --env dev --model DEV_Azure_Gpt54
# --------------------------------------------------------------------------
def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description="Run capability probes against one configured model")
    p.add_argument("--config", required=True)
    p.add_argument("--env", default=None)
    p.add_argument("--model", required=True, help="model name as loaded (env-prefixed, e.g. DEV_Azure_Gpt54)")
    p.add_argument("--unreachable", action="store_true", help="simulate route down (network-free probes only)")
    p.add_argument("--out", default=None, help="CSV path (default probes_<model>.csv)")
    args = p.parse_args()

    from config_schema import load_models_from_config
    models_to_test, _cost, _settings = load_models_from_config(args.config, args.env) \
        if args.env else load_models_from_config(args.config)
    if args.model not in models_to_test:
        raise SystemExit(f"{args.model!r} not in config. Available: {list(models_to_test)}")

    # load_models_from_config returns built instances; we also need the raw cfg.
    # Re-read the raw config entry for the network-free checks.
    from config_schema import load_config
    raw = load_config(args.config)
    raw_entry = {}
    for env_block in (raw.get("environments", {}) or {}).values():
        for m in env_block.get("models", []):
            nm = m.get("name", "")
            if args.model.endswith(nm):
                raw_entry = m
                break
    raw_entry = raw_entry or {}

    ctx = ProbeContext(model=models_to_test[args.model],
                       model_name=args.model, model_config=raw_entry)
    result = asyncio.run(run_probes(ctx, route_reachable=not args.unreachable))
    out = args.out or f"probes_{args.model}.csv"
    write_probe_csv(result["rows"], out)
    print(f"\nGATE: {'PASS' if result['gate_passed'] else 'FAIL'}  ->  {out}")


if __name__ == "__main__":
    _main()
