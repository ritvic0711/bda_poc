# Bedrock Enrichment — Metrics Reference

How every column produced by `bedrock_enrich.py` is calculated, what it tells you,
and why some cells come back empty.

Running example used throughout — one row from the aggregated report:

| Model Name | Model ID | Account |
|---|---|---|
| `anthropic.claude-3-haiku` | `us.anthropic.claude-3-haiku` | `gc1-pdo-np` |

Assume a 30-day window and these (invented) facts for the worked numbers:
- The model is in **Legacy** state.
- Over 30 days it received **90,000 invocations**, **1,200,000,000 input tokens**, **300,000,000 output tokens**, and **1,500 throttles**.
- Its single busiest minute consumed **50,000** burndown-adjusted tokens.
- Your applied TPM limit is **2,000,000**; applied RPM limit is **400**.

---

## 1. Column summary

| Column | Source | Type | One-line meaning |
|---|---|---|---|
| `lifecycle_status` | ListFoundationModels | fetched | ACTIVE / LEGACY / EOL / UNKNOWN |
| `is_legacy` | derived | computed | `status == "LEGACY"` |
| `eol_date` | Health API → model-card | fetched | Retirement date (Legacy/EOL only) |
| `applied_rpm` | Service Quotas | fetched | Your requests-per-minute ceiling |
| `applied_tpm` | Service Quotas | fetched | Your tokens-per-minute ceiling |
| `invocations` | CloudWatch | fetched | Total successful calls in window |
| `input_tokens` | CloudWatch | fetched | Total prompt tokens in window |
| `output_tokens` | CloudWatch | fetched | Total generated tokens in window |
| `throttles` | CloudWatch | fetched | Total throttled (429) requests in window |
| `peak_est_tpm_use` | CloudWatch | fetched | Worst single minute of quota consumption |
| `tpm_pct_of_quota` | derived | computed | `peak / applied_tpm × 100` |
| `enrich_region` | derived | computed | Region actually queried for this row |
| `enrich_note` | derived | computed | Why a cell is blank / assumptions made |

"fetched" = pulled from an API. "computed" = calculated in the script. Only three
values are truly computed; everything else is a lookup.

---

## 2. Lifecycle metrics

### `lifecycle_status`
**Source:** one `ListFoundationModels` call, read from each model's `modelLifecycle.status`
field. The script matches your Model ID (geo-prefix stripped) against the returned catalogue.

**Tells you:** whether AWS still fully supports the model.
- `ACTIVE` — fully supported.
- `LEGACY` — scheduled for retirement; still callable but migrate before EOL. New customers can't adopt it.
- `EOL` — past end-of-life; calls will fail.
- `UNKNOWN` — the ID didn't match the catalogue (see §7).

**Example:** `us.anthropic.claude-3-haiku` → stripped to `anthropic.claude-3-haiku` → matches the catalogue entry whose status is `LEGACY`. Result: **`LEGACY`**.

### `is_legacy`
**Calculated:** `lifecycle_status == "LEGACY"` → boolean.

**Example:** status is LEGACY → **`True`**.

---

## 3. EOL date

### `eol_date`
**Source:** two-tier, only attempted for LEGACY/EOL models (Active models have no EOL date by design).
1. **AWS Health API** (primary) — reads Bedrock deprecation notices, extracts the date, indexes it by model ID.
2. **Model-card page scrape** (fallback) — fetches `docs.aws.amazon.com/.../model-card-<provider>-<model>.html` and greps for the EOL line.

**Tells you:** the date after which calls to this model will fail. Migrate before it.

**Example:** the Health map contains `{'anthropic.claude-3-haiku': 'September 10, 2026'}`, so the lookup returns **`September 10, 2026`** with no web fetch needed.

**Note:** Bedrock EOL dates are specific to Bedrock and can differ from the provider's own
(Anthropic/Cohere) schedule. Only the Bedrock date governs your Bedrock calls.

---

## 4. Quota metrics (these are LIMITS, not usage)

### `applied_rpm` / `applied_tpm`
**Source:** `list_service_quotas` for `ServiceCode=bedrock`, read from each quota's **applied
value** (your real limit including any increases AWS granted — not the public default).
Matched to the model by name: Bedrock quota names embed the display name, e.g.
`"On-demand model inference tokens per minute for Anthropic Claude 3 Haiku"`.

**Tells you:** your per-minute ceiling. These are the **denominators** for headroom — not a
measurement of what you consumed.

**Example:**
- `applied_rpm` → matches the "...requests per minute for Anthropic Claude 3 Haiku" quota → **`400`**.
- `applied_tpm` → matches the "...tokens per minute..." quota → **`2,000,000`**.

---

## 5. Utilisation metrics (CloudWatch, namespace `AWS/Bedrock`)

All four are **window totals, not rates**. Each is one `get_metric_statistics` call filtered
to the model's `ModelId`, with `Period=86400` (daily buckets) and `Statistics=["Sum"]`; the
script then **sums the daily values** across the window.

```
metric_total = Σ (daily Sum for each day in the window)
```

### `invocations`
**Metric:** `Invocations` — count of *successful* requests (Converse, ConverseStream,
InvokeModel, InvokeModelWithResponseStream). Failed and throttled calls are NOT included.

**Tells you:** how much you actually used the model.

**Example:** 3,000/day × 30 days → **`90,000`**.

### `input_tokens`
**Metric:** `InputTokenCount`. Total prompt tokens sent over the window.

**Example:** **`1,200,000,000`**.

### `output_tokens`
**Metric:** `OutputTokenCount`. Total generated tokens over the window.

**Example:** **`300,000,000`**.

### `throttles`
**Metric:** `InvocationThrottles`. Count of requests rejected (429) for crossing a per-minute
limit.

**Tells you:** whether you actually hit a ceiling. This is the *confirmation* signal.

**Example:** **`1,500`** — you were throttled 1,500 times in 30 days, so you ARE capacity-constrained at peaks.

---

## 6. Peak consumption and headroom

### `peak_est_tpm_use`
**Source:** `EstimatedTPMQuotaUsage` metric, but queried differently — `Period=60`
(per-minute buckets), `Statistics=["Maximum"]`, then the script takes the **max across every
minute** in the window.

Why minute-level + max: TPM limits are enforced **per minute**, so your worst minute is what
determines throttling, not your total or average. AWS computes this metric as:

```
EstimatedTPMQuotaUsage = InputTokenCount + CacheWriteInputTokens + (OutputTokenCount × burndown_rate)
```

This is the **burndown-adjusted** figure AWS meters against your quota — output tokens are
weighted, so it is NOT simply `input_tokens + output_tokens`.

**Example:** busiest minute → **`50,000`**.

### `tpm_pct_of_quota` — THE ONLY MEANINGFUL DERIVED METRIC
**Calculated:**

```
tpm_pct_of_quota = round(100 × peak_est_tpm_use / applied_tpm, 1)
```

**Tells you:** worst-minute consumption as a percentage of your ceiling = your throttling-risk
headroom.

**Example:** `100 × 50,000 / 2,000,000` = **`2.5%`** — tons of headroom.

**How to read it alongside throttles:**

| `tpm_pct_of_quota` | `throttles` | Interpretation |
|---|---|---|
| Low (e.g. 2.5%) | 0 | Comfortable, no action |
| High (>80%) | 0 | Close to the edge; plan a quota increase |
| High | >0 | Confirmed TPM-constrained; request increase now |
| Low | >0 | Spiky bursts within the minute, OR an **RPM** (not TPM) limit is the binding one |

Only computed when BOTH `peak_est_tpm_use` and `applied_tpm` are present; otherwise blank.

---

## 7. Region + diagnostics

### `enrich_region`
**Calculated:** region inferred from the Model ID geo-prefix — `us.*`→`us-east-1`,
`eu.*`→`eu-west-1`, `apac.*`→`ap-southeast-1`. For `global.*` and bare IDs there's no region to
parse, so it uses `default_region`.

**Tells you:** which region's CloudWatch/quota data this row reflects. Matters because all
utilisation and quota numbers are **per-region**.

**Example:** `us.` prefix → **`us-east-1`**.

### `enrich_note`
**Calculated:** free-text flags explaining assumptions or gaps — e.g. "no lifecycle match",
"EOL unresolved", "global model: quota/util reflect us-east-1 only", "account not mapped to a
profile: quota/util skipped". **Read this column first** when a value looks wrong or empty.

---

## 8. Why values come back empty for some models

Blank cells are almost always one of these, and the cause is usually in `enrich_note`.

### A. The `ModelId` logging mismatch (most common cause of empty utilisation)
CloudWatch logs usage under the **exact** `ModelId` string your application called. The script
queries using the full ID from your sheet (geo prefix included). If your code invoked a
*different* form than the sheet lists — e.g. you called the bare `anthropic.claude-3-haiku`
but the sheet row is `global.anthropic.claude-3-haiku`, or vice versa — the metric query finds
no series and every utilisation column (`invocations`, tokens, `throttles`, `peak_est_tpm_use`)
comes back **0 or blank**, even though you know the model is in use.
**Fix:** make the sheet's Model ID match the exact string used at call time.

### B. No traffic in the window
CloudWatch only has datapoints for days with activity. If the model wasn't called in the last
`days` days (default 30), the sums are legitimately **0**. Widen the window if you expect older
traffic.

### C. `EstimatedTPMQuotaUsage` not emitted
This metric is relatively new and only emits for recent traffic. Older or low-volume models
show blank `peak_est_tpm_use` → which also blanks `tpm_pct_of_quota` (can't divide without it).

### D. Credentials not mapped → quotas/utilisation skipped
If you pass `account_to_profile` and the row's `Account` isn't in it, the script does lifecycle
+ EOL but **skips all live calls** (quotas + utilisation). Note reads:
`"account not mapped to a profile: quota/util skipped"`.
**Fix:** add the account→profile mapping, or pass `account_to_profile=None` to use the default
credential chain for all rows.

### E. Wrong region for a `global.` model
`global.` models can't be region-pinned, so they're queried in `default_region` only. If the
model's real traffic is in another region, that region's CloudWatch has the data and the
queried region shows **0**. Note reads: `"global model: quota/util reflect <region> only"`.
**Fix:** set `default_region` to where the traffic actually lands, or query per-region.

### F. Quota name didn't match → blank `applied_rpm`/`applied_tpm`
Quota matching is fuzzy against the model's display name. Oddly-named or brand-new models may
not match any quota string, leaving these blank even though a limit exists.
**Fix:** verify with `aws service-quotas list-service-quotas --service-code bedrock` and, if
needed, hardcode the quota for that model.

### G. Lifecycle didn't match → `UNKNOWN` status, no EOL
If the geo-stripped Model ID isn't in the `ListFoundationModels` catalogue (deprecated ID,
typo, region where the model isn't offered), status is `UNKNOWN` and EOL is skipped. Note
reads: `"no lifecycle match"`.

### Quick triage table

| Empty column(s) | Most likely cause | Where to confirm |
|---|---|---|
| All utilisation (invocations, tokens, throttles) | A (ID mismatch) or B (no traffic) | Compare sheet ID vs. called ID |
| Only `peak_est_tpm_use` + `tpm_pct_of_quota` | C (metric not emitted) | Check metric exists in CloudWatch |
| Quotas + utilisation together | D (no profile) | `enrich_note` |
| `applied_rpm` / `applied_tpm` only | F (quota name miss) | Service Quotas console |
| `lifecycle_status`=UNKNOWN, no `eol_date` | G (no catalogue match) | `enrich_note` |
| Utilisation on a `global.` model | E (wrong region) | `enrich_note` |
