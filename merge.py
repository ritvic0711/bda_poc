#!/usr/bin/env python3
"""
Merge aggregated_report_*.csv into one file, one row per model.

- Column names are normalized to a single schema (handles snake_case vs Title Case).
- Any column not in the canonical list is dropped (no extra columns).
- environment lists ALL envs a model is seen in (e.g. "qa,dev"), priority-ordered.
- All other values come from the single highest-priority env (prod > qa > dev).

Usage:
    python merge_reports.py -i ./reports -o merged_report.xlsx
    python merge_reports.py -i ./reports -o merged.csv -p "aggregated_report_*.csv"
"""
import argparse
import glob
import os
import re

import pandas as pd

KEY_COLS = ["model_name", "model_id"]
ENV_PRIORITY = ["prod", "qa", "dev"]          # first = most preferred

# canonical column -> accepted header aliases (normalized: lowercase, alnum only)
ALIASES = {
    "model_name":      ["modelname"],
    "model_id":        ["modelid"],
    "environment":     ["environment"],
    "model_provider":  ["modelprovider", "provider"],
    "avg_latency_sec": ["avglatencysec", "avglatency"],
    "avg_ttft_sec":    ["avgttftsec", "avgttft"],
    "avg_throughput":  ["avgthroughput", "avgthroughputtps"],
    "json_compliance": ["jsoncompliance"],
    "total_run_cost":  ["totalruncost"],
    "total_tokens":    ["totaltokens"],
    "invoke":          ["invoke"],
    "stream":          ["stream"],
    "tool_calling":    ["toolcalling"],
    "reasoning":       ["reasoning"],
    "agent_loop":      ["agentloop"],
    "multiturn_tools": ["multiturntools", "multiturn"],
    # add "account": ["account"], "vendor": ["vendor"], here if you want to keep them
}
# final column order
OUTPUT_ORDER = ["model_name", "model_id", "environment", "model_provider",
                "avg_latency_sec", "avg_ttft_sec", "avg_throughput",
                "json_compliance", "total_run_cost", "total_tokens",
                "invoke", "stream", "tool_calling", "reasoning",
                "agent_loop", "multiturn_tools"]

_norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
LOOKUP = {a: canon for canon, al in ALIASES.items() for a in al}


def normalize(df):
    """Rename known columns to canonical names, drop the rest, coalesce duplicates."""
    df = df.rename(columns={c: LOOKUP[_norm(c)] for c in df.columns if _norm(c) in LOOKUP})
    df = df[[c for c in df.columns if c in ALIASES]]           # drop unknown columns
    if df.columns.duplicated().any():                          # coalesce same-named cols
        df = pd.DataFrame({c: df.loc[:, df.columns == c].bfill(axis=1).iloc[:, 0]
                           for c in pd.unique(df.columns)})
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input-dir", default=".")
    ap.add_argument("-o", "--output", default="merged_report.xlsx")
    ap.add_argument("-p", "--pattern", default="aggregated_report_*.csv")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.input_dir, a.pattern)))
    if not files:
        raise SystemExit(f"No files matched {a.pattern} in {a.input_dir}")

    frames = []
    for f in files:
        df = normalize(pd.read_csv(f))
        frames.append(df)
        print(f"loaded {os.path.basename(f)}: {len(df)} rows")

    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["model_name"])
    df["environment"] = df["environment"].astype(str).str.strip().str.lower()

    rank = {e: i for i, e in enumerate(ENV_PRIORITY)}
    df["_rank"] = df["environment"].map(rank).fillna(len(ENV_PRIORITY)).astype(int)
    df["_order"] = range(len(df))

    # all envs a model appears in, ordered by priority -> "qa,dev"
    env_join = (df.sort_values("_rank")
                  .groupby(KEY_COLS)["environment"]
                  .agg(lambda s: ",".join(dict.fromkeys(s))))

    # keep one row per model: best env; within that env, the latest row (no averaging)
    best = (df.sort_values(["_rank", "_order"], ascending=[True, False])
              .drop_duplicates(subset=KEY_COLS, keep="first")
              .set_index(KEY_COLS))
    best["environment"] = env_join            # overwrite single env with the full list
    out = best.reset_index()

    out = out[[c for c in OUTPUT_ORDER if c in out.columns]]
    out = out.sort_values(["model_name", "model_id"])

    if a.output.lower().endswith(".csv"):
        out.to_csv(a.output, index=False)
    else:
        out.to_excel(a.output, index=False)
    print(f"\nwrote {a.output}: {len(out)} models, {out.shape[1]} columns")


if __name__ == "__main__":
    main()