# Local PoC — change set

Three files touched. `source.py`, `base.py`, `aggregation.py`, `tasks.py`, `llm_client.py` are **unchanged**.

---

## 1. NEW — `askgartner_conversation_logic/evaluation/local_poc.py`

The whole Langfuse replacement in one file: `Evaluation`, no-op `observe`, no-op `CallbackHandler`, and `run_local_experiment` / `run_local_experiment_sync` which dump `results/<experiment>-<run_name>/results.json`.

---

## 2. EDIT — `evaluation/metrics/validation_faithfulness_metric.py`

Two import lines. **Nothing else in the file changes** — `_judge`, `faithfulness_judgments`, the rubric and all three `@run_evaluator`s stay exactly as they are.

```diff
-from langfuse import Evaluation, observe
-from langfuse.langchain import CallbackHandler
+from askgartner_conversation_logic.evaluation.local_poc import (
+    CallbackHandler,
+    Evaluation,
+    observe,
+)
```

Lines 8–9 in your current file. Everything else — `init_chat_model`, `get_model_config`, `MetricBase`, `run_evaluator`, `NA_SCORE`, `valid_scores`, pydantic — imports as before.

The `config={"callbacks": [CallbackHandler()]}` inside `_judge` can stay; it's now a no-op. Delete it if you'd rather.

---

## 3. EDIT — `tests/evals/test_validation_faithfulness.py`

Full replacement attached. What changed:

| Removed | Why |
|---|---|
| `langfuse` fixture from the signature | nothing to connect to |
| `ensure_langfuse_dataset` import + call | the DataFrame *is* the dataset locally |
| `from ...dataset.dataset import ensure_langfuse_dataset` | `dataset.py` is unused on this path |
| `from ...runners.langfuse import run_dataset_experiment` | → `local_poc.run_local_experiment_sync` |
| `reporter` fixture | it's shaped for a Langfuse result; `print` instead |

Downsampling moved inline — `queries.sample(downsample, random_state=downsample_seed)` — since the `_n<limit>_s<seed>` dataset-name suffixing only existed to avoid clobbering the uploaded dataset.

`metric.langfuse_run_evaluators` is still called by that name, since `MetricBase` is untouched.

---

## Where the CSV comes from

`source.py` needs **no change**. Its `load_dataset` already falls back to local: with `APP_ENVIRONMENT` unset, `dataset_bucket()` raises inside the `try`, the `except Exception` fires, and it reads

```
askgartner_conversation_logic/evaluation/dataset/local/validation_faithfulness.csv
```

So just drop the CSV there. You'll see the `S3 read for %s failed ... using local fallback` warning — that's the intended path, not a problem.

---

## Run it

```bash
pytest tests/evals/test_validation_faithfulness.py -q -s
pytest tests/evals/test_validation_faithfulness.py -q -s --downsample 5
```

`results/validation-faithfulness-<run_name>/results.json`:

```json
{
  "item_count": 50,
  "error_count": 0,
  "metrics": { "avg_faithfulness": 0.42, "faithful_rate": 0.76, "severe_hallucination_rate": 0.08 },
  "items": [ { "row_index": 0, "input": {...}, "output": {...},
               "evaluations": [ { "name": "faithfulness", "value": 0.0, "comment": "<judge justification>" } ] } ]
}
```

---

## Two things to check against your repo

1. **`valid_scores`** — my `ItemResult` exposes `.evaluations` as a list of objects with `.name` / `.value`, which is the Langfuse shape. If your `aggregation.valid_scores` reaches for something else (`.scores`, a dict), adjust `ItemResult` in `local_poc.py` rather than the metric.
2. **`validate_document(bot, item)`** — the runner passes each DataFrame row as a plain dict keyed by `_INPUT_COLS`. If your task expects a Langfuse item object and reaches for `item.input`, either unwrap it in the task or change `_run_one` to pass `{"input": row}`.
