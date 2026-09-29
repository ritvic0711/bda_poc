#!/usr/bin/env python3
"""
Merge aggregated_report_*.csv files into one file, one row per model.
If a model is in several environments, the row from the preferred env is used
(prod > qa > dev). Values are copied as-is; no new columns, no averaging.

Usage:
    python merge_reports.py -i ./reports -o merged_report.xlsx
    python merge_reports.py -i ./reports -o merged.csv -p "aggregated_report_*.csv"
"""
import argparse
import glob
import os

import pandas as pd

KEY_COLS = ["model_name", "model_id"]
ENV_PRIORITY = ["prod", "qa", "dev"]   # first = most preferred


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
        df = pd.read_csv(f)
        df.columns = [c.strip() for c in df.columns]
        frames.append(df)
        print(f"loaded {os.path.basename(f)}: {len(df)} rows")

    df = pd.concat(frames, ignore_index=True)
    columns = list(df.columns)                      # original columns, original order

    rank = {e: i for i, e in enumerate(ENV_PRIORITY)}
    env = df["environment"].astype(str).str.strip().str.lower()
    df["_rank"] = env.map(rank).fillna(len(ENV_PRIORITY)).astype(int)
    df["_order"] = range(len(df))

    # best env first; inside the same env, the latest row wins (no averaging)
    df = df.sort_values(["_rank", "_order"], ascending=[True, False])
    out = df.drop_duplicates(subset=KEY_COLS, keep="first")

    out = out.sort_values(["model_name", "model_id"])[columns]

    if a.output.lower().endswith(".csv"):
        out.to_csv(a.output, index=False)
    else:
        out.to_excel(a.output, index=False)

    print(f"\nwrote {a.output}: {len(out)} models")
    print(out["environment"].value_counts().to_string())


if __name__ == "__main__":
    main()