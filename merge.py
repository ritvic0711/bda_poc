#!/usr/bin/env python3
"""
Merge aggregated_report_*.csv files into one file, one row per model,
with per-environment columns (dev / qa / prod / ...).

Usage:
    python merge_reports.py --input-dir ./reports --output merged_report.xlsx
    python merge_reports.py -i ./reports -o merged.csv --pattern "aggregated_report_*.csv"
"""
import argparse
import ast
import glob
import os

import pandas as pd

# ---- config ---------------------------------------------------------------
KEY_COLS = ["model_name", "model_id"]          # identity of a model
STATIC_COLS = ["model_provider"]               # same across envs, kept once
STATUS_COLS = ["invoke", "stream", "tool_calling", "reasoning",
               "agent_loop", "multiturn_tools"]
ENV_ORDER = ["dev", "qa", "prod"]              # others get appended alphabetically
TREAT_ZERO_AS_MISSING = ["avg_ttft_sec"]       # 0 here = not measured, not "instant"
# ---------------------------------------------------------------------------


def parse_status(val):
    """Handles 'PASS', "('PASS', '')", "('FAIL', 'Agent loop ...')" -> (status, reason)."""
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

    # split status tuples into status + reason
    for c in STATUS_COLS:
        if c not in df.columns:
            continue
        parsed = df[c].apply(parse_status)
        df[c] = parsed.apply(lambda x: x[0])
        df[f"{c}__reason"] = parsed.apply(lambda x: x[1])
    return df


def agg_status(s):
    s = s.dropna()
    if s.empty:
        return None
    return "FAIL" if (s == "FAIL").any() else s.iloc[0]   # any FAIL wins


def agg_reasons(s):
    r = sorted({x for x in s if x})
    return " | ".join(r)


def collapse_duplicates(df):
    """Same model + same env appearing multiple times (re-runs) -> one row."""
    meta = set(KEY_COLS + STATIC_COLS + ["environment", "source_file"])
    reason_cols = [f"{c}__reason" for c in STATUS_COLS if f"{c}__reason" in df.columns]
    status_cols = [c for c in STATUS_COLS if c in df.columns]
    num_cols = [c for c in df.columns
                if c not in meta and c not in status_cols + reason_cols]

    agg = {c: "mean" for c in num_cols}
    agg.update({c: agg_status for c in status_cols})
    agg.update({c: agg_reasons for c in reason_cols})
    agg.update({c: "first" for c in STATIC_COLS if c in df.columns})
    agg["source_file"] = lambda s: ", ".join(sorted(set(s)))

    g = df.groupby(KEY_COLS + ["environment"], dropna=False).agg(agg).reset_index()
    return g, num_cols, status_cols, reason_cols


def pivot_envs(g, num_cols, status_cols, reason_cols):
    metric_cols = num_cols + status_cols
    envs = sorted(g["environment"].unique(),
                  key=lambda e: (ENV_ORDER.index(e) if e in ENV_ORDER else 99, e))

    # static info: first non-null per model
    static = [c for c in STATIC_COLS if c in g.columns]
    base = g.groupby(KEY_COLS, dropna=False)[static].first().reset_index()

    wide = base
    for m in metric_cols:
        for e in envs:
            sub = g.loc[g["environment"] == e, KEY_COLS + [m]].rename(columns={m: f"{m}_{e}"})
            wide = wide.merge(sub, on=KEY_COLS, how="left")

    # consolidate fail reasons into one column per env (keeps sheet narrow)
    for e in envs:
        sub = g.loc[g["environment"] == e, KEY_COLS + reason_cols].copy()
        def join_row(r):
            parts = [f"{c.replace('__reason','')}: {r[c]}" for c in reason_cols if r[c]]
            return " | ".join(parts)
        sub[f"fail_reasons_{e}"] = sub.apply(join_row, axis=1)
        wide = wide.merge(sub[KEY_COLS + [f"fail_reasons_{e}"]], on=KEY_COLS, how="left")

    src = g.groupby(KEY_COLS, dropna=False)["source_file"].agg(
        lambda s: ", ".join(sorted({x for v in s for x in v.split(", ")}))).reset_index()
    wide = wide.merge(src, on=KEY_COLS, how="left")
    wide["environments_present"] = wide[[f"{status_cols[0]}_{e}" for e in envs]].notna() \
        .apply(lambda r: ",".join(e for e, ok in zip(envs, r) if ok), axis=1) if status_cols else ""
    return wide


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input-dir", default=".")
    ap.add_argument("-o", "--output", default="merged_report.xlsx")
    ap.add_argument("-p", "--pattern", default="aggregated_report_*.csv")
    a = ap.parse_args()

    df = clean(load_all(a.input_dir, a.pattern))
    g, num_cols, status_cols, reason_cols = collapse_duplicates(df)
    wide = pivot_envs(g, num_cols, status_cols, reason_cols)

    if a.output.lower().endswith(".csv"):
        wide.to_csv(a.output, index=False)
    else:
        wide.to_excel(a.output, index=False)
    print(f"\nwrote {a.output}: {len(wide)} models x {wide.shape[1]} columns")


if __name__ == "__main__":
    main()