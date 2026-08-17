# Best strategy for production — and why

**Short answer: built-in *semantic* strategy with overrides (`customMemoryStrategy` → `semanticOverride`), paired with a plain built-in `userPreferenceMemoryStrategy`.** Use plain built-ins for anything without a hard business constraint; reach for overrides only where you need control.

That is a deliberately narrow recommendation. Here's the reasoning, including where it's wrong.

## The four options, ranked for production

| Strategy | Control | Cost to your acct | Latency | Maintenance | Verdict |
|---|---|---|---|---|---|
| `semanticMemoryStrategy` (built-in) | low | none (service-managed) | low | none | Great default for facts |
| `userPreferenceMemoryStrategy` (built-in) | low | none | low | none | Best-in-class for personalization; keep as built-in |
| `summaryMemoryStrategy` (built-in) | low | none | low | none | Fine for session recap; override only for handoff format |
| `customMemoryStrategy` (override) | **high** | **billed to you** | higher | you own a prompt | Use where control/compliance matters |

## Why semantic-with-overrides wins *for a business-rules-driven system*

1. **Deterministic scope.** The built-in semantic extractor keeps whatever it judges durable. In production that means noise, opinions, and transient state leak into LTM, which then pollutes retrieval. An `extraction.appendToPrompt` constrains it to *your* schema (plan, entitlements, integrations, open issues) so retrieval precision goes up and token cost on every downstream call goes down.

2. **PII / compliance control.** You can instruct extraction to never persist card numbers, IDs, or PII beyond a first name. With a plain built-in you're trusting the managed prompt; with an override it's an auditable line you own. For anything regulated, that auditability is the whole ballgame.

3. **Consolidation you can reason about.** The `consolidation.appendToPrompt` decides add-vs-update-vs-skip. Overriding it stops memory drift and duplicate facts (e.g. "REPLACE plan tier on conflict"). Built-in consolidation is a black box you can't tune when it makes the wrong call.

4. **Pinned model = reproducibility.** Override lets you pin `modelId`. Same input → same extraction behavior across deploys, and you choose the cost/quality point (Haiku for cheap extraction, Sonnet where accuracy matters).

## Where built-ins beat overrides (don't over-reach)

- **Cost.** Built-ins are service-managed — **no inference billed to your account**. Overrides invoke Bedrock in *your* account for every extraction and consolidation, plus you need a memory execution role. If you have no business constraint on a strategy, overriding it just adds cost and a prompt to maintain for no gain. That's why `userPreferenceMemoryStrategy` stays a plain built-in above.
- **Latency & failure surface.** Overrides add an LLM hop you own; a bad prompt edit can silently degrade extraction quality. Built-ins can't.
- **Ops load.** Every override is a prompt you now version, test, and regression-check. Only pay that where it buys control.

## The rule of thumb

> Start every strategy as a plain built-in. Promote a strategy to `customMemoryStrategy` **only** when a concrete business or compliance requirement forces it — constrained schema, PII handling, or consolidation logic you must be able to explain. Never override for "it might be nicer."

## Concrete production config for this POC

- **Semantic** → override (extraction constrains schema + strips PII; consolidation replaces-on-conflict). Model: Haiku via cross-region inference profile.
- **User preference** → plain built-in. Cheap, managed, already good.
- **Summary** → override *only if* you need a specific handoff format; otherwise built-in.
- **STM** → 30-day event retention, rehydrate last 6 turns, business-rule preamble injected at startup.

## On "change prompts on demand"

- **LTM prompts are hot-swappable** via `UpdateMemory` — no need to recreate the memory resource. New prompts apply to **subsequent** extractions; already-extracted records are not retroactively re-processed. `demo.py --sync` does exactly this.
- **STM has no extraction prompt to change** (it's verbatim). The STM equivalent of a "prompt change" is editing the system preamble in `business_rules.yaml`, which applies on the very next turn — no API call at all.
