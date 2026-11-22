"""Local PoC shims — the only new file. Drop at:

    askgartner_conversation_logic/evaluation/local_poc.py

Replaces the three things Langfuse was providing:
  * `langfuse.Evaluation`              -> dataclass below (same name/value/comment)
  * `langfuse.observe`                 -> no-op decorator
  * `langfuse.langchain.CallbackHandler`-> no-op class
  * `runners.langfuse.run_dataset_experiment` -> `run_local_experiment`

Nothing here imports langfuse. Results go to `<results_root>/<run>/results.json`.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Langfuse stand-ins
# --------------------------------------------------------------------------
@dataclass
class Evaluation:
    """Same shape as `langfuse.Evaluation` so metric code is unchanged."""

    name: str
    value: float | int | str | bool | None
    comment: str | None = None


def observe(*_args: Any, **_kwargs: Any):
    """No-op `@observe`. Accepts and ignores name/capture_input/capture_output."""

    def decorator(fn):
        return fn

    # tolerate bare @observe as well as @observe(name=...)
    if len(_args) == 1 and callable(_args[0]) and not _kwargs:
        return _args[0]
    return decorator


class CallbackHandler:
    """No-op stand-in so `config={"callbacks": [CallbackHandler()]}` can stay."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass
class ItemResult:
    """What run evaluators receive. `.evaluations` matches the Langfuse shape,
    so `metrics.utils.aggregation.valid_scores` should work unchanged."""

    row_index: int
    input: dict[str, Any]
    output: Any = None
    evaluations: list[Evaluation] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0


@dataclass
class RunResult:
    run_name: str
    item_results: list[ItemResult] = field(default_factory=list)
    run_evaluations: list[Evaluation] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.item_results if r.error)


def _normalize(raw: Any, fallback_name: str) -> list[Evaluation]:
    if raw is None:
        return []
    if isinstance(raw, Evaluation):
        return [raw]
    if isinstance(raw, (int, float, bool, str)):
        return [Evaluation(name=fallback_name, value=raw)]
    if isinstance(raw, dict):
        return [Evaluation(**raw)]
    out: list[Evaluation] = []
    for entry in raw:
        out.extend(_normalize(entry, fallback_name))
    return out


async def _call(fn: Callable, /, **kwargs: Any) -> Any:
    """Pass only the kwargs `fn` accepts; await if needed."""
    try:
        sig = inspect.signature(fn)
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except (TypeError, ValueError):
        pass
    result = fn(**kwargs)
    return await result if inspect.isawaitable(result) else result


async def _run_one(
    idx: int, row: dict, task: Callable, evaluators: Sequence[Callable],
    sem: asyncio.Semaphore, total: int,
) -> ItemResult:
    async with sem:
        started = time.time()
        result = ItemResult(row_index=idx, input=row)
        try:
            if inspect.iscoroutinefunction(task):
                result.output = await task(row)
            else:
                result.output = await asyncio.to_thread(task, row)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Task failed on row %s", idx)
            result.duration_s = time.time() - started
            return result

        for evaluator in evaluators:
            name = getattr(evaluator, "__name__", "evaluation")
            try:
                raw = await _call(evaluator, output=result.output, expected_output=None,
                                  input=row, metadata={"row_index": idx})
                result.evaluations.extend(_normalize(raw, name))
            except Exception as exc:
                logger.exception("Evaluator %s failed on row %s", name, idx)
                result.evaluations.append(
                    Evaluation(name=name, value=None, comment=f"evaluator error: {exc}")
                )

        result.duration_s = time.time() - started
        logger.info("[%d/%d] %s", idx + 1, total,
                    {e.name: e.value for e in result.evaluations})
        return result


async def run_local_experiment(
    queries: pd.DataFrame,
    task: Callable,
    evaluators: Sequence[Callable] = (),
    run_evaluators: Sequence[Callable] = (),
    max_concurrency: int = 4,
    run_name: str | None = None,
    experiment_base_name: str = "experiment",
    results_root: str | Path = "results",
    config: dict[str, Any] | None = None,
) -> RunResult:
    """Run `task` over each DataFrame row, score it, dump JSON. No Langfuse.

    `queries` replaces the uploaded dataset — the DataFrame *is* the dataset,
    so `ensure_langfuse_dataset` / `create_dataset_from_queries` are not needed.
    """
    started = time.time()
    run_name = run_name or time.strftime("%Y%m%d-%H%M%S")
    experiment_name = f"{experiment_base_name}-{run_name}"
    rows = queries.to_dict(orient="records")
    logger.info("Running '%s' over %d rows (max_concurrency=%d)",
                experiment_name, len(rows), max_concurrency)

    sem = asyncio.Semaphore(max_concurrency)
    item_results = await asyncio.gather(
        *[_run_one(i, row, task, evaluators, sem, len(rows)) for i, row in enumerate(rows)]
    )

    result = RunResult(run_name=run_name, item_results=list(item_results))
    for evaluator in run_evaluators:
        name = getattr(evaluator, "__name__", "run_metric")
        try:
            raw = await _call(evaluator, item_results=result.item_results)
            result.run_evaluations.extend(_normalize(raw, name))
        except Exception as exc:
            logger.exception("Run evaluator %s failed", name)
            result.run_evaluations.append(
                Evaluation(name=name, value=None, comment=f"run evaluator error: {exc}")
            )

    result.duration_s = time.time() - started
    _write_json(result, experiment_name, Path(results_root), config or {})
    return result


def _write_json(result: RunResult, experiment_name: str, root: Path, config: dict) -> None:
    run_dir = root / experiment_name
    run_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "run_name": result.run_name,
        "experiment_name": experiment_name,
        "item_count": len(result.item_results),
        "error_count": result.error_count,
        "duration_s": round(result.duration_s, 2),
        "config": config,
        "metrics": {e.name: e.value for e in result.run_evaluations},
        "items": [
            {
                "row_index": r.row_index,
                "input": r.input,
                "output": r.output,
                "evaluations": [asdict(e) for e in r.evaluations],
                "error": r.error,
                "duration_s": round(r.duration_s, 3),
            }
            for r in result.item_results
        ],
    }
    path = run_dir / "results.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote %s", path)


def run_local_experiment_sync(*args: Any, **kwargs: Any) -> RunResult:
    """Blocking wrapper, so the pytest eval can stay a plain `def` like the original."""
    return asyncio.run(run_local_experiment(*args, **kwargs))


__all__ = [
    "Evaluation", "observe", "CallbackHandler",
    "ItemResult", "RunResult",
    "run_local_experiment", "run_local_experiment_sync",
]
