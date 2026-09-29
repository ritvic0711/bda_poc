#!/usr/bin/env python3
"""
Merge aggregated_report_*.csv files into one file: one row per model.
If a model exists in multiple environments, only the highest-priority env is kept
(default: prod > qa > dev).

Usage:
    python merge_reports.py -i ./reports -o merged_report.xlsx
    python merge_reports.py -i ./reports -o merged.csv -p "aggregated_report_*.csv"
"""
import argparse
import ast
import glob
import os

import pandas as pd

# ---- config ---------------------------------------------------------------
KEY_COLS = ["model_name", "model_id"]
ENV_PRIORITY = ["prod", "qa", "dev"]          # first = most preferred
STATUS_COLS = ["invoke", "stream", "tool_calling", "reasoning",
               "agent_loop", "multiturn_tools"]
TREAT_ZERO_AS_MISSING = ["avg_ttft_sec"]      # 0 = not measured
# ---------------------------------------------------------------------------


def parse_status(val):
    """'PASS', "('PASS', '')", "('FAIL', 'reason')" -> (status, reason)."""
    if pd.isna(val):
        return None, ""
    s = str(val).strip()
    if s.startswith("(") and s.endswith(")"):
        try:
            t = ast.literal_eval(s)
            return str(t[0]).strip().upper(), (str(t[1]).strip() if len(t) > 1 else "")
        except Exception:
            pass
    return s.upper(), ""


def load_all(input_dir, pattern):
    files = sorted(glob.glob(os.path.join(input_dir, pattern)))
    if not files:
        raise SystemExit(f"No files matched {pattern} in {input_dir}")
    frames = []
    for f in files:
        df = pd.read_csv(f)
        df.columns = [c.strip() for c in df.columns]
        df["source_file"] = os.path.basename(f)
        frames.append(df)
        print(f"loaded {os.path.basename(f)}: {len(df)} rows")
    return pd.concat(frames, ignore_index=True)


def clean(df):
    df["environment"] = df["environment"].astype(str).str.strip().str.lower()

    for c in TREAT_ZERO_AS_MISSING:
        if c in df.columns:
            df[c] = df[c].replace(0, pd.NA)

    for c in STATUS_COLS:
        if c not in df.columns:
            continue
        parsed = df[c].apply(parse_status)
        df[c] = parsed.apply(lambda x: x[0])
        df[f"{c}__reason"] = parsed.apply(lambda x: x[1])
    return df


def select_preferred_env(df):
    """Keep only the highest-priority environment per model."""
    rank = {e: i for i, e in enumerate(ENV_PRIORITY)}
    df["_rank"] = df["environment"].map(rank).fillna(len(ENV_PRIORITY)).astype(int)

    # record which envs each model was seen in (for transparency)
    seen = (df.groupby(KEY_COLS)
              .apply(lambda x: ",".join(sorted(set(x["environment"]),
                                               key=lambda e: rank.get(e, 99))),
                     include_groups=False)
              .rename("environments_available").reset_index())

    best = df.groupby(KEY_COLS)["_rank"].transform("min")
    df = df[df["_rank"] == best].copy()
    df = df.merge(seen, on=KEY_COLS, how="left")
    return df.drop(columns="_rank")


def agg_status(s):
    s = s.dropna()
    if s.empty:
        return None
    return "FAIL" if (s == "FAIL").any() else s.iloc[0]


def agg_reasons(s):
    return " | ".join(sorted({x for x in s if x}))


def collapse_duplicates(df):
    """Re-runs of same model in the chosen env -> one row."""
    status_cols = [c for c in STATUS_COLS if c in df.columns]
    reason_cols = [f"{c}__reason" for c in status_cols]
    first_cols = [c for c in df.columns
                  if c in ("environment", "environments_available")
                  or (c not in KEY_COLS + status_cols + reason_cols + ["source_file"]
                      and not pd.api.types.is_numeric_dtype(df[c]))]
    num_cols = [c for c in df.columns
                if c not in KEY_COLS + status_cols + reason_cols + first_cols + ["source_file"]]

    agg = {c: "mean" for c in num_cols}
    agg.update({c: agg_status for c in status_cols})
    agg.update({c: agg_reasons for c in reason_cols})
    agg.update({c: "first" for c in first_cols})
    agg["source_file"] = lambda s: ", ".join(sorted(set(s)))

    out = df.groupby(KEY_COLS, dropna=False).agg(agg).reset_index()

    # merge reasons into a single column
    def join_reasons(r):
        return " | ".join(f"{c.replace('__reason', '')}: {r[c]}" for c in reason_cols if r[c])
    out["fail_reasons"] = out.apply(join_reasons, axis=1)
    return out.drop(columns=reason_cols)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input-dir", default=".")
    ap.add_argument("-o", "--output", default="merged_report.xlsx")
    ap.add_argument("-p", "--pattern", default="aggregated_report_*.csv")
    a = ap.parse_args()

    df = clean(load_all(a.input_dir, a.pattern))
    df = select_preferred_env(df)
    out = collapse_duplicates(df)

    # nicer column order
    front = ["model_name", "model_id", "environment", "environments_available"]
    out = out[front + [c for c in out.columns if c not in front]]

    if a.output.lower().endswith(".csv"):
        out.to_csv(a.output, index=False)
    else:
        out.to_excel(a.output, index=False)
    print(f"\nwrote {a.output}: {len(out)} models")
    print(out["environment"].value_counts().to_string())


if __name__ == "__main__":
    main()