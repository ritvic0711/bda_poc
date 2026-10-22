# bedrock_enrich — How Each Column Is Fetched / Calculated

Reference for the current version of `bedrock_enrich.py`, which adds **six columns** to your
dataframe: `lifecycle_status`, `is_legacy`, `eol_date`, `inference_type`, `allocated_rpm`,
`allocated_tpm`. It reports **allocated quotas + lifecycle only** — no CloudWatch, no usage.

Running example — one row:

| Model Name | Model ID | Account |
|---|---|---|
| `anthropic.claude-3-haiku` | `us.anthropic.claude-3-haiku` | `gc1-pdo-np` |

---

## Overview: what runs, and how often

| Step | API call | Frequency | Needs creds |
|---|---|---|---|
| Lifecycle catalogue | `bedrock:ListFoundationModels` | **once** total | any one profile |
| EOL dates (Health) | `health:DescribeEvents` + `DescribeEventDetails` | **once** total | any one profile (+ Business/Ent. support) |
| EOL dates (fallback) | HTTP GET of model-card page | per model, cached | none |
| Allocated quotas | `servicequotas:ListServiceQuotas` | **once per (profile, region)**, cached | the row's mapped profile |

No per-row AWS calls for lifecycle/EOL — those come from the one-time catalogue + map. Only the
quota list is fetched per region, and it's cached so each region is pulled once regardless of how
many rows use it.

---

## 1. `lifecycle_status`  — *fetched*

**How:** a single `ListFoundationModels` call builds a catalogue of every model with its
`modelLifecycle.status`. Each row's Model ID is matched against it by `match_lifecycle()`.

**Matching** (`match_lifecycle`): the Model ID is geo-stripped first (`us.anthropic.claude-3-haiku`
→ `anthropic.claude-3-haiku`), then matched exact → version-prefix → loose-containment against the
catalogue's `modelId` values.

**Value:** the catalogue entry's `status` field, verbatim: `ACTIVE`, `LEGACY`, or `EOL`. If no
catalogue entry matches, `UNKNOWN`.

**Example:** `us.anthropic.claude-3-haiku` → stripped → matches catalogue entry with
`status=LEGACY` → **`LEGACY`**.

---

## 2. `is_legacy`  — *calculated*

**How:** `is_legacy = (lifecycle_status == "LEGACY")`. Pure boolean derivation, no call.

**Example:** status is LEGACY → **`True`**.

---

## 3. `eol_date`  — *fetched (two-tier), only for LEGACY/EOL*

Active models are skipped entirely — AWS doesn't publish an EOL date until a model goes Legacy, so
there's nothing to fetch.

**Tier 1 — AWS Health API (primary):** `build_health_eol_map()` calls `describe_events`
(`services=["BEDROCK"]`, categories scheduledChange + accountNotification), then
`describe_event_details` for each. From each notice description it regex-extracts a date and indexes
it by every model-ID token found in the text → `{'anthropic.claude-3-haiku': 'September 10, 2026'}`.
Pinned to `us-east-1` (Health's only endpoint). If the account has no Business/Enterprise support
plan, it raises `SubscriptionRequiredException`, which is caught → returns `{}` and silently falls to Tier 2.

**Tier 2 — model-card scrape (fallback):** `eol_from_card()` builds the doc URL from provider +
model name (`model-card-anthropic-claude-3-haiku.md`), HTTP-GETs it, and greps for a line containing
"EOL"/"end-of-life", then extracts the date. Cached per model.

**Date parsing** (`_extract_date`): matches `September 10, 2026`, `10 September 2026`, or `2026-09-10`.

**Dispatcher** (`resolve_eol`): tries the Health map first (in-memory, free), falls back to the card
scrape only if not found.

**Example:** `anthropic.claude-3-haiku` is in the Health map → returns **`September 10, 2026`** with
no web fetch.

---

## 4. `inference_type`  — *calculated (from the Model ID alone)*

**How:** `inference_type_for_row()` geo-strips the Model ID and checks for a prefix.
- Any geo prefix (`us.`, `eu.`, `apac.`, `global.`, …) → the call goes through a cross-Region
  inference profile → **`cross-region`**.
- A bare ID with no prefix → single-Region on-demand → **`on-demand`**.

No API call — it's read purely from the ID string. This also decides *which TPM quota* governs the
row (see §6).

**Example:** `us.anthropic.claude-3-haiku` has the `us.` prefix → **`cross-region`**.
A bare `anthropic.claude-sonnet-5` → **`on-demand`**.

---

## 5. `allocated_rpm`  — *fetched*

**How:** `get_applied_quotas()` pages `list_service_quotas` for `ServiceCode=bedrock` in the row's
region, returning `[(QuotaName, Value), …]` where `Value` is your **applied** (allocated) limit —
i.e. including any increases AWS granted, not the public default. `find_quota(..., kind="requests")`
then name-matches the model.

**RPM specifics:** there is a **single** RPM quota per model — `InvokeModel requests per minute for
<model>` — with no cross-region/on-demand split. **Some models have no RPM quota at all** (e.g. Opus
4.7/4.8); those return `None` (blank), with a `verbose` note "no RPM quota (some models have none)".

**Example:** matches `InvokeModel requests per minute for Anthropic Claude 3 Haiku` → **`400`**.

---

## 6. `allocated_tpm`  — *fetched, scope-aware*

**How:** same quota list as RPM, but TPM has **two separate quotas** per model and the script must
pick the one that actually governs the call:
- `Cross-Region InvokeModel tokens per minute for <model>`
- `On-demand InvokeModel tokens per minute for <model>`

`find_quota(..., kind="tokens", inference_type=...)` selects by the row's `inference_type`:
cross-region rows take the Cross-Region quota, on-demand rows take the On-demand quota. If the exact
scope isn't found, it falls back to whichever exists and logs "no cross-region TPM quota; showing
on-demand".

**Example:** the Haiku row is `cross-region` → picks `Cross-Region InvokeModel tokens per minute for
Anthropic Claude 3 Haiku` → **`4,000,000`** (not the 1,000,000 on-demand value).

---

## The quota name-matcher (shared by §5 and §6)

Because quota names embed the model's display name, matching has to be careful not to let one model
borrow another's quota. Two-stage:

**Stage 1 — scope/kind filter** (`find_quota`): keep only quota names that contain
- the right `… per minute` phrase (`requests` for RPM, `tokens` for TPM), AND
- `invokemodel` or `model inference` (accepts both current and legacy naming).

**Stage 2 — distinctive-token match** (`_name_matches`): tokenise the model's **display name** (from
the catalogue, not the ID), drop generic tokens (`claude`, `anthropic`, `gpt`, `nova`, …), and
require **every remaining distinctive token** (`haiku`, `sonnet`, `opus`, version numbers) to appear
in the quota name. This is why "Claude Opus 4 8" won't match a "Claude 3 Haiku" quota — `opus`/`4`/`8`
aren't in it.

**Display name source:** the matcher uses the catalogue's `modelName` ("Claude 3 Haiku"), not your
sheet's `Model Name` (often the ID form `anthropic.claude-3-haiku`, which has no distinctive display
tokens). The ID-shaped name is used only when the catalogue gave none.

---

## Region handling (internal — no longer a column)

`region_for_row()` infers the region to query for quotas from the Model ID prefix: `us.`→`us-east-1`,
`eu.`→`eu-west-1`, `apac.`→`ap-southeast-1`. For `global.` and bare IDs there's no parseable region,
so it uses `default_region` (default `us-east-1`). Quotas are per-region, so a `global.` model's
quota reflects only that one region — flagged in the `verbose` log, not a column.

---

## Credentials and the account mapping

`account_to_profile={"gc1-pdo-np": "<aws-profile>"}` maps each row's `Account` alias to a real AWS
profile for the Service Quotas call. Behaviour:
- **Mapped:** quotas fetched normally.
- **Not in the mapping:** lifecycle + EOL + inference_type still populate (they don't need the row's
  creds), but `allocated_rpm`/`allocated_tpm` are blank, logged as "account not mapped to a profile:
  quotas skipped".
- **`account_to_profile=None`:** the default credential chain is used for every row — fine when all
  rows are one account you're already authenticated to.

---

## Why a quota value comes back empty

| Symptom | Cause | Confirm with |
|---|---|---|
| Both quotas blank, note "quotas skipped" | Account not mapped to a profile | pass `verbose=True` |
| `allocated_rpm` blank only | Model genuinely has no RPM quota (Opus 4.7/4.8) | Service Quotas console |
| `allocated_tpm` blank | Quota name didn't match the model | `verbose=True`, check console |
| `lifecycle_status=UNKNOWN`, no `eol_date` | Model ID not in the catalogue | `verbose=True` |
| `eol_date` blank on a Legacy model | Health has no plan + card scrape missed | AWS Health Dashboard |
| Wrong region's quota for a `global.` model | `global.` can't be region-pinned | set `default_region` |

Run with `verbose=True` to get the per-row reason on stderr — that's where the old `enrich_note`
information now goes.

---

## Honest caveats

1. **Quota matching is name-based and fuzzy.** It's conservative (won't cross-assign models), but a
   model whose AWS quota name differs from its catalogue display name can come back blank. Spot-check
   against `aws service-quotas list-service-quotas --service-code bedrock`.
2. **EOL dates are best-effort.** Health needs a support plan; the card scrape breaks if AWS renames
   a card page. Verify anything load-bearing against the Health Dashboard.
3. **`global.` quotas are single-region.** The number reflects `default_region` only, not true global
   allocation.
