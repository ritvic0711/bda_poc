# LLM Multi-Provider Async Load & Capability Testing Suite

Benchmarks LLM models across AWS Bedrock, Azure OpenAI, and GCP Gemini from a
single YAML config, computes latency/token/cost metrics, and (separately) runs
per-model capability tests (invoke / stream / tool-calling / reasoning / agent).

---

## 1. Contents

| File | Role |
|------|------|
| `async_testing_suite.py` | Main benchmark orchestrator (load config → run all models concurrently → metrics → CSV reports). |
| `config_schema.py` | Config loader + model factory. Resolves `${ENV_VARS}`, builds LangChain models via `init_chat_model`, builds the cost matrix. |
| `metrics_config.yaml` | All metric names + formulas (measurements / per-run / aggregate). No code to change a metric. |
| `metrics_engine.py` | Evaluates the formulas in `metrics_config.yaml`. |
| `test_azure_langchain.py` / `test_bedrock_langchain.py` / `test_gcp_langchain.py` | Standalone capability tests, model built inline. |
| `configs/<env>/<vendor>.yaml` | **Current** config layout: one file per vendor, environments as keys inside. |
| `model_config.yaml` (or `configs/<vendor>_<env>.yaml`) | **Older** layout: one flat file per vendor+env. |
| `export.env` | Environment variables (endpoints, keys, Langfuse). Not committed. |
| `aws_bedrock_discovery.py` | Optional: pull provisioned Bedrock models + list prices from AWS. |

---

## 2. Setup

```bash
python -m venv venv
source venv/bin/activate

pip install \
  langchain langchain-core langchain-openai langchain-aws langchain-google-genai \
  langgraph pandas tiktoken pyyaml simpleeval

# optional
pip install boto3                       # AWS Bedrock + discovery
pip install langfuse                    # observability (auto-skipped if absent)
pip install aws-bedrock-token-generator # only for bedrock_mantle auth
```

Load environment variables before any run:

```bash
set -a; source export.env; set +a
```

### Required env vars

Azure (current per-env layout — one set per environment):
```
AZURE_QA_APIM_HOST_URL     AZURE_DEV_APIM_HOST_URL
AZURE_QA_API_KEY           AZURE_DEV_API_KEY
AZURE_QA_SUBSCRIPTION_KEY  AZURE_DEV_SUBSCRIPTION_KEY
AZURE_QA_LEGACY_URL        AZURE_DEV_LEGACY_URL
AZURE_QA_OPENAI_API_KEY    AZURE_DEV_OPENAI_API_KEY
```
GCP:      `GOOGLE_API_KEY`
AWS:      standard AWS creds (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`, or a profile); region lives in the YAML.
Langfuse (optional): `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, `LANGFUSE_ENABLED=true|false`.

---

## 3. Input data

The suite reads a CSV of documents to run each model against. Columns used:
- `res_id`     → doc id (falls back to `row_<index>`)
- `summary_txt`→ context text (truncated to 15k chars)

Path comes from `--input`, else `test_settings.input_csv`, else `input_file.csv`.

---

## 4. Running the benchmark — `async_testing_suite.py`

### Current layout (config file per vendor, environments inside)

Config resolves to `configs/<env>/<vendor>.yaml`.

```bash
# by vendor + env (path auto-built from --config-dir)
python async_testing_suite.py --vendor azure --env qa

# explicit path (equivalent)
python async_testing_suite.py --config configs/qa/azure.yaml --vendor azure --env qa

# multiple environments in one run (names get env-prefixed: QA_..., DEV_...)
python async_testing_suite.py --config configs/qa/azure.yaml --vendor azure --env "qa,dev"

# other vendors
python async_testing_suite.py --vendor aws --env qa
python async_testing_suite.py --vendor gcp --env dev
```

### Older layout (one flat file per vendor+env, or the default)

```bash
# default config (model_config.yaml)
python async_testing_suite.py

# a specific flat config
python async_testing_suite.py --config model_config.yaml
python async_testing_suite.py --config configs/azure_qa.yaml
```

### Common overrides (work with either layout)

```bash
# single model only — MUST match the name as loaded.
# In multi-env mode names are prefixed, e.g. QA_Azure_Gpt54, DEV_Azure_Gpt54
python async_testing_suite.py --vendor azure --env qa --model QA_Azure_Gpt54

# iterations per (model x document)
python async_testing_suite.py --vendor azure --env qa --iterations 5

# max concurrent API calls (semaphore bound)
python async_testing_suite.py --vendor azure --env qa --max-concurrent 10

# custom input / output CSV
python async_testing_suite.py --vendor azure --env qa \
  --input custom_data.csv --output custom_results.csv
```

### All flags

| Flag | Short | Default | Meaning |
|------|-------|---------|---------|
| `--vendor` | `-v` | none | aws \| azure \| gcp |
| `--env` | `-e` | none | environment(s); comma-separated for multi-env (`"qa,dev"`) |
| `--config-dir` | | `configs` | base dir holding `<env>/<vendor>.yaml` |
| `--config` | `-c` | `model_config.yaml` | explicit config path (wins if it exists) |
| `--input` | `-i` | config/`input_file.csv` | input CSV |
| `--output` | `-o` | config/`async_benchmark_results...csv` | per-run output CSV |
| `--iterations` | `-n` | config/`3` | runs per model per document |
| `--max-concurrent` | `-m` | config/`10` | concurrency ceiling |
| `--model` | | none | run only this one model (by loaded name) |

**Resolution order:** if `--config` exists it's used as-is; else if `--vendor`+`--env`
given, path = `configs/<env>/<vendor>.yaml`; else falls back to `--config`.

### Outputs

- `aggregated_report_*.csv` — per-model summary (grouped by name/model_id/environment/provider).
- `error_report.csv` — every failed run (model, doc_id, error).
- per-run CSV (`--output`) — one row per (model × doc × iteration) with all metrics.

---

## 5. Config files

### Current: `configs/<env>/<vendor>.yaml` (environments inside)

One vendor file holds every environment under `environments:`. Uses YAML anchors
so each model is defined once and each environment only supplies its endpoint.

```yaml
vendor: azure
_base: &base            # shared: provider, model_type, model_provider
_stream_temp: &stream_temp
_endpoints:             # PER-ENV url + key + subscription key (qa vs dev differ here)
  qa_apim: &ep_qa_apim { base_url: "${AZURE_QA_APIM_HOST_URL}", api_key: "${AZURE_QA_API_KEY}", ... }
  dev_apim: &ep_dev_apim { base_url: "${AZURE_DEV_APIM_HOST_URL}", ... }
_catalog:               # each model ONCE — model id + cost, NO endpoint, NO headers
  azure_gpt54: &azure_gpt54 { <<: *base_apim, <<: *stream_temp, model: "...", cost: {...} }
environments:
  qa:
    models:
      - { <<: [*azure_gpt54, *ep_qa_apim], name: "Azure_Gpt54" }
  dev:
    models:
      - { <<: [*azure_gpt54, *ep_dev_apim], name: "Azure_Gpt54" }
```

Run it: `--vendor azure --env qa` (or `--env "qa,dev"`).
In `<<: [*model, *endpoint]` the **first anchor wins on key clash** — keep all
per-env values (url/key/subscription key) in the endpoint anchor only.

### Older: one flat file per vendor+env

Single top-level `models:` list, endpoints baked into each entry. No `environments:` key.

```yaml
test_settings: {}
models:
  - name: "AWS_Bedrock_Claude_Haiku"
    provider: "aws"
    model_type: "bedrock_runtime"
    model_provider: "bedrock_converse"
    model_id: "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    region: "us-east-1"
    streaming: true
    temperature: 0
    cost: { input_per_1m: 1.00, output_per_1m: 5.00 }
```

Run it: `--config configs/azure_qa.yaml` (no `--env` needed; it has no `environments:`).

---

## 6. Model instantiation — `config_schema.py`

Not run directly; imported by the suite. What it does:
- `resolve_env_vars()` — recursively expands `${ENV_VAR}` everywhere in the config.
- `load_config()` — reads + env-resolves the YAML.
- `load_models_from_config(path, envs)` — parses `environments:`, env-prefixes model
  names (`QA_...`), builds instances + cost matrix. Returns `(models_to_test, cost_matrix, test_settings)`.
- `build_model_instance(cfg)` — strips framework fields (`name`, `provider`,
  `model_type`, `cost`, `auth_method`) and passes the rest to `init_chat_model()`.
- `_AUTH_HANDLERS` — special auth (e.g. `bedrock_mantle_iam` generates an IAM token).

Sanity-check a config without running the whole suite:

```bash
python -c "from config_schema import load_models_from_config as L; \
m,c,t = L('configs/qa/azure.yaml', envs='qa'); print(list(m)); print(list(c))"
```

To add a param to every call: use the exact `init_chat_model` name in the YAML
(e.g. `max_tokens`, `top_p`, `default_headers`) — it passes straight through.

---

## 7. Metrics — `metrics_config.yaml` + `metrics_engine.py`

All metric names and formulas live in `metrics_config.yaml`. Change a metric =
edit YAML, no Python. `metrics_engine.py` just evaluates it.

Three layers:
- `measurements` — extract raw values from the live run (latency, ttft, tokens, cost rates).
- `per_run` — derived per-call metrics (tpot, throughput, estimated_cost_usd) → one CSV row.
- `aggregate` — per-model summary (avg latency, json compliance rate, total cost).

The suite wires it via `METRICS = MetricsEngine("metrics_config.yaml")` and calls
`METRICS.compute_per_run(sources, helpers)` per run and
`METRICS.compute_aggregate(df_success, group_by=[...])` for the summary.

Add a metric — e.g. cache hit rate — with zero code:
```yaml
per_run:
  - name: cache_hit_rate
    formula: cached_tokens / max(prompt_tokens, 1)
    round: 3
```

Validate the config loads and see the columns it will emit:
```bash
python -c "from metrics_engine import MetricsEngine as M; \
e=M('metrics_config.yaml'); print(e.per_run_field_names())"
```

---

## 8. Capability tests — `test_azure_langchain.py` (+ bedrock / gcp)

Standalone. Model built **inline** in `build_model()` (edit the file to change it).
Runs five scenarios: invoke, stream, tool-calling, reasoning (prompt-based),
agent loop (LangGraph). Logs each + a PASS/FAIL summary.

```bash
# set the env vars the script reads, then:
python test_azure_langchain.py
python test_bedrock_langchain.py
python test_gcp_langchain.py
```

Env vars (standalone scripts, non-prefixed):
- Azure: `APIM_HOST_URL`, `AZURE_OPENAI_API_KEY` (+ `AZURE_SUBSCRIPTION_KEY` if used)
- GCP: `GOOGLE_API_KEY`
- AWS: standard AWS creds

To change model/params, edit the active (uncommented) args in that file's
`build_model()`; every other `init_chat_model` param is listed commented below it.

---

## 9. Optional: dynamic Bedrock discovery — `aws_bedrock_discovery.py`

Pull provisioned-throughput models + list prices from your AWS account.
Needs `boto3` and IAM perms `bedrock:ListProvisionedModelThroughputs`,
`pricing:GetProducts`. Enable via the `auto_discover.bedrock_provisioned` block
in the AWS config. Prices are AWS **list** prices (reference), not provisioned billing.

---

## 10. Troubleshooting

- **404 `Endpoint does not exist ... include Openai-model and Openai-model-version`**
  Azure APIM requires per-request headers `Openai-model` and `Openai-model-version`
  (plus `Ocp-Apim-Subscription-Key`). `Openai-model` is per-model (deployment name),
  the subscription key is per-env — compose them into `default_headers` in
  `build_model_instance` (per-model header + per-env key), since YAML `<<` can't
  deep-merge two `default_headers` dicts.
- **404 on legacy embedding URL** — check the resolved `base_url`; the per-model
  path suffix must be substituted before the call. `print(params)` before
  `init_chat_model(**params)` shows the exact URL going out.
- **All models missing headers/keys after a refactor** — a per-env value ended up
  in a catalog anchor; the model anchor wins the merge and overrides the endpoint.
  Move it back to the endpoint anchor.
- **Langfuse noise / failures** — set `LANGFUSE_ENABLED=false` or leave the keys
  unset; observability is auto-skipped.
- **Model not found with `--model`** — in multi-env runs names are prefixed
  (`QA_...`, `DEV_...`). Use the prefixed name.
