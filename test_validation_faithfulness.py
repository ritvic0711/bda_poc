"""tests/evals/test_validation_faithfulness.py — local PoC variant.

Changes vs. the Langfuse version:
  - no `langfuse` fixture, no `ensure_langfuse_dataset`: the DataFrame IS the
    dataset, so the upload/cache round-trip is gone
  - `run_dataset_experiment` -> `run_local_experiment_sync`
  - scores land in `results/<experiment>-<run_name>/results.json`
Every other import is untouched.
"""

import functools

import pytest

from askgartner_conversation_logic.evaluation.dataset.source import load_dataset
from askgartner_conversation_logic.evaluation.local_poc import run_local_experiment_sync
from askgartner_conversation_logic.evaluation.metrics.validation_faithfulness_metric import (
    ValidationFaithfulnessMetric,
)
from askgartner_conversation_logic.evaluation.tasks import validate_document

_INPUT_COLS = ("instruction", "user_profile", "res_id", "doc_title", "document", "original_question", "query")


@pytest.mark.evaluation
@pytest.mark.validation
def test_validation_faithfulness(
    bot, hydra_cfg, max_concurrency, downsample, downsample_seed, run_name
):
    """The validate node's summaries must stay grounded in the source document."""
    queries = load_dataset("validation_faithfulness.csv")[list(_INPUT_COLS)]
    if downsample is not None and len(queries) > downsample:
        queries = queries.sample(downsample, random_state=downsample_seed)
    print(f"running on {len(queries)} rows")

    config = hydra_cfg["validation_faithfulness"]
    metric = ValidationFaithfulnessMetric(
        model_params=config["model_params"],
        system_prompt=config["system_prompt"],
        user_template=config["user_template"],
    )

    result = run_local_experiment_sync(
        queries=queries,
        task=functools.partial(validate_document, bot),
        evaluators=[metric.faithfulness_judgments],
        run_evaluators=metric.langfuse_run_evaluators,
        max_concurrency=max_concurrency,
        run_name=run_name,
        experiment_base_name="validation-faithfulness",
        config={"model_params": config["model_params"], "downsample": downsample},
    )

    run_evaluations = {e.name: e.value for e in result.run_evaluations}
    print(run_evaluations)

    assert result.error_count == 0, f"{result.error_count} items errored"
    assert run_evaluations["avg_faithfulness"] < 2.0
