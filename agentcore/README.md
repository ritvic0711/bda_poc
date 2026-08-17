# AgentCore Memory — prompt customization POC (STM + LTM)

Standalone POC. Nothing here touches gAuth/GCOM. It does four things the note asked for:

1. An agent wired to AgentCore Memory with configurable strategies — create the memory and test it.
2. Custom **business rules injected as prompt overrides** into long-term memory.
3. **All strategies explored in code** (`poc/strategies.py`, `python -m poc.demo --catalog`).
4. A **single separate place** — `config/business_rules.yaml` — where you edit prompts / rules; on startup the system uses them for **both** short-term and long-term memory, and you can change the LTM ones on demand.

The production recommendation ("which strategy is best and why") is in **[RECOMMENDATION.md](RECOMMENDATION.md)**.

## The one thing to understand first: where prompt customization actually applies

AgentCore Memory has two tiers, and they are **not** symmetric:

- **Short-term memory (STM)** = raw, verbatim conversation events. **AgentCore runs no LLM over STM**, so there is *no extraction prompt to customize*. "Customizing STM" means three concrete levers, all in the config file: the system-prompt **preamble** (your business rules), how many **turns** you rehydrate, and event **retention**.
- **Long-term memory (LTM)** = extracted + consolidated records. **This is where prompt customization is real.** Each strategy exposes an `extraction.appendToPrompt` and/or `consolidation.appendToPrompt` that overrides the built-in instructions (the output schema stays fixed). Set it → the strategy becomes "built-in with overrides" (`customMemoryStrategy`).

So the single config file feeds both tiers — just through different mechanisms. That's the honest version of "custom prompt for STM and LTM."

## The "separate place": `config/business_rules.yaml`

Everything you'd want to change lives in one human-editable file:

- `stm.system_preamble` — business rules injected into the agent every turn (the STM "prompt")
- `stm.rehydrate_turns`, `memory.retention_days` — STM behavior
- `ltm.<strategy>.extraction.append_to_prompt` / `consolidation.append_to_prompt` — the LTM prompt overrides
- `memory.ltm_model_id` — model used when a strategy is overridden

On startup, `poc/memory.py::ensure_memory()` reads this file, builds the strategy config (`poc/strategies.py`), and creates the memory resource. To change LTM prompts **on demand** without recreating anything, edit the file and run `python -m poc.demo --sync` → it calls `UpdateMemory`. STM preamble edits need no API call; they apply next turn.

## All strategies (explored in `poc/strategies.py`)

| # | Strategy | Steps | Managed cost | Notes |
|---|---|---|---|---|
| 1 | `semanticMemoryStrategy` | extract + consolidate | service-managed | durable facts |
| 2 | `userPreferenceMemoryStrategy` | extract + consolidate | service-managed | preferences/styles |
| 3 | `summaryMemoryStrategy` | consolidate only | service-managed | per-session summary |
| 4 | `customMemoryStrategy` → `semanticOverride` / `userPreferenceOverride` / `summaryOverride` | your prompt + model | **billed to your account** | built-in-with-overrides |

`python -m poc.demo --catalog` prints these at runtime.

## Layout

```
config/business_rules.yaml   # <- the single place you edit
poc/settings.py              # env + paths (role ARN from env only)
poc/rules.py                 # load yaml -> STM preamble + shared accessors
poc/strategies.py            # ALL strategy builders + build_strategies_from_rules()
poc/memory.py                # create/update/poll + STM events + LTM retrieval
poc/agent.py                 # assembles preamble + LTM + STM turns per message
poc/demo.py                  # end-to-end runnable demo
RECOMMENDATION.md            # best strategy for production + why
```

## Run

```bash
pip install -r requirements.txt
export AWS_REGION=us-west-2                     # match memory.region in the yaml
export MEMORY_EXECUTION_ROLE_ARN=arn:aws:iam::<acct>:role/<memory-exec-role>   # only if using overrides

python -m poc.demo --catalog     # see every strategy
python -m poc.demo               # create memory from rules, seed a session, retrieve STM+LTM
python -m poc.demo --live        # also call Bedrock for the agent's replies
python -m poc.demo --sync        # push edited LTM prompt overrides on demand
```

Notes:
- Override strategies need an **AgentCore memory execution role** (Bedrock `InvokeModel` + a trust policy for `bedrock-agentcore.amazonaws.com`). Plain built-ins don't.
- LTM extraction is **asynchronous** — the demo waits ~60s before retrieving records.
- The resolved `memory_id` is cached in `.memory_id` so reruns reuse the resource; delete it to force a fresh create.
- Session IDs are ≥33 chars (an AgentCore requirement).
