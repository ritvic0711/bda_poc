# Bedrock Data Automation — Pipeline Reference

A conceptual companion to the POC notebooks. Covers the **standard** vs **custom** pipelines, what **segments** and **elements** are, what the **output JSON** looks like and **where it's saved** in S3, and how **`invoke_data_automation_async`** actually works.

> All JSON keys are snake_case. Treat the exact field names below as "verify against your own run" — the surest source of truth is `print(json.dumps(standard_outputs[0], indent=2)[:3000])`. BDA versions can shift minor keys.

---

## 1. The two pipelines

BDA has exactly two output modes. A single job can run **one or both** at the same time.

| | **Standard pipeline** | **Custom pipeline** |
|---|---|---|
| **What it does** | Generic structured extraction of *whatever is in the document* — text, tables, figures, summaries | Extracts *specific named fields you define* via a blueprint schema |
| **You configure** | `standardOutputConfiguration` (granularity, formats, summaries) | `customOutputConfiguration` (which blueprint ARNs to apply) |
| **Blueprint needed?** | No | Yes — a JSON schema you author (or an AWS catalog blueprint) |
| **Output shape** | Fixed, document-agnostic structure (`document` / `pages` / `elements`) | *Your* schema's fields (`inference_result`) |
| **Answers** | "What's in this document?" | "What is the claim number / total / patient name?" |
| **Page limit** | up to 3,000 pages (with splitter) | ~20 pages per sub-document, ≤100 fields |
| **In the notebook** | text/tables/figures sections | §7 (claim blueprint) |

**Key mental model:** the standard pipeline *reads the document*; the custom pipeline *fills in a form you designed*. They are complementary — run standard to get the full content, run custom to pull the handful of fields you'll query on.

### How they combine

Both are attached to one **project**. One `invoke` then produces, per segment, a standard-output JSON **and** (if a blueprint matched) a custom-output JSON.

```python
# standard only  -> generic extraction
customOutputConfiguration = None
# custom too     -> ALSO get your named fields, routed per sub-document
customOutputConfiguration = {"blueprints": [{"blueprintArn": bp_arn, "blueprintStage": "LIVE"}]}
```

---

## 2. Segments

A **segment** = one *logical sub-document* the **splitter** carved out of your input file.

- Splitter **off** → the whole file is **one** segment (max 20 pages).
- Splitter **on** → BDA classifies the file into multiple logical documents and emits **one segment per sub-document**, each ≤20 pages, up to 3,000 pages total.

Boundaries are **ML-inferred** from content and layout cues (document-type changes, form headers, page-number restarts, structural shifts) — you don't set them manually; your only lever is splitter on/off.

### Example

`claims-pack.pdf` (10 pages) might split into 3 segments:

| Segment | Source pages | Logical document |
|---|---|---|
| 0 | 1–4 | Claim form A |
| 1 | 5–7 | Claim form B |
| 2 | 8–10 | Explanation of Benefits |

→ You get **3 standard-output JSONs** (one per segment), and — with the claim blueprint attached — **custom output on the segments that matched** (`MATCH`), while the EOB might come back `NO_MATCH`.

> **Provenance tip:** each segment's JSON records its original source page range in `metadata.start_page_index` / `end_page_index`. Use that for citations — it's more reliable than the per-segment `page_index`, which may restart at 0 inside each segment.

---

## 3. Elements

Within one segment's standard output, an **element** is a single structural piece of the document. Every element has a `type`:

| `type` | What it is | Useful fields |
|---|---|---|
| `TEXT` | a paragraph / text block | `representation.markdown`, `representation.text` |
| `TABLE` | a detected table | `representation.csv`, `representation.html`, `title`, `summary`, `csv_s3_uri` |
| `FIGURE` | an embedded image / chart / logo | `crop_images` (S3 URIs of the cropped image), `summary` (caption) |

Every element also carries:
- `reading_order` — its sequence in the document flow
- `locations` — `[{ "page_index": n, "bounding_box": {top,left,width,height} }]` — where it physically sits

Elements only appear when **`ELEMENT` granularity** is enabled. Without it you get flowing markdown but no discrete tables/figures.

---

## 4. Standard output JSON — structure

**One file per segment.** Four top-level blocks map to the four granularity levels:

```jsonc
{
  "metadata": {
    "semantic_modality": "DOCUMENT",
    "start_page_index": 0,        // source page range in the ORIGINAL file (provenance)
    "end_page_index": 3,
    "number_of_pages": 4
  },

  "document": {                   // DOCUMENT level — the whole segment
    "representation": { "text": "...", "markdown": "...", "html": "..." },
    "statistics": { "element_count": 12, "table_count": 2, "figure_count": 1 },
    "summary": "One generative paragraph describing THIS sub-document."   // summary is PER DOCUMENT
  },

  "pages": [                      // PAGE level — one entry per page
    {
      "page_index": 0,
      "representation": { "text": "...", "markdown": "..." },   // page text only — NO summary
      "asset_metadata": { "rectified_image": "s3://..." }        // if additionalFileFormat ON
    }
  ],

  "elements": [                   // ELEMENT level — every table / figure / text block
    {
      "type": "TABLE",
      "reading_order": 5,
      "locations": [ { "page_index": 1, "bounding_box": { "top": 0.4, "left": 0.1, "width": 0.8, "height": 0.2 } } ],
      "representation": { "markdown": "| .. |", "html": "<table>..</table>", "csv": "col1,col2\n.." },
      "title": "Service Lines",
      "summary": "Table of billed procedures.",
      "csv_s3_uri": "s3://..."     // if additionalFileFormat ON
    },
    {
      "type": "FIGURE",
      "locations": [ { "page_index": 0, "bounding_box": { } } ],
      "summary": "Provider logo, top-left.",
      "crop_images": [ "s3://.../figure_0.png" ]   // the actual cropped image
    }
  ],

  "text_lines": [ ],              // only if LINE granularity enabled
  "text_words": [ ]               // only if WORD granularity enabled
}
```

**Things to internalize:**
- The generative **`summary` is per document (per segment), not per page.** Pages hold text, no summary.
- **Tables** → use `representation.csv` / `.html` (load straight into pandas).
- **Figures** → `crop_images` gives you the extracted image files; `summary` is the caption.

---

## 5. Custom output JSON — structure

**One file per segment, only when `custom_output_status == "MATCH"`.** This is your blueprint's fields.

```jsonc
{
  "matched_blueprint": {
    "arn": "arn:aws:bedrock:...:blueprint/poc-health-claim",
    "name": "poc-health-claim",
    "confidence": 0.97                       // how sure BDA was about routing to this blueprint
  },
  "document_class": { "type": "HealthInsuranceClaim" },

  "inference_result": {                      // YOUR extracted fields (what §7 prints)
    "claim_number": "CLM-0001",
    "patient_name": "Jane Doe",
    "total_charge": 1234.56,
    "balance_due": 234.56,                   // an 'inferred' field
    "service_lines": [                       // a 'list' field
      { "date_of_service": "2025-01-10", "cpt_code": "99213", "description": "Office visit", "charge": 150.00 }
    ]
  },

  "explainability_info": [                   // per-field confidence + location (great for QA)
    { "claim_number": { "success": true, "confidence": 0.98, "geometry": [ { "page": 0, "boundingBox": { } } ] } }
  ]
}
```

Two fields worth wiring into a real pipeline: **`explainability_info`** (threshold on per-field `confidence` to auto-flag low-confidence extractions for human review) and **`matched_blueprint.confidence`** (routing certainty).

---

## 6. Where output is saved (S3 layout)

Everything lands under the `outputConfiguration.s3Uri` prefix you passed on invoke. Driven by one index file:

```
s3://<bucket>/bda/output/<invocation-id>/
├── job_metadata.json                     ← the INDEX (read this first, always)
├── standard_output/
│   ├── 0/result.json                     ← segment 0 standard output
│   ├── 1/result.json                     ← segment 1
│   └── 2/result.json
├── custom_output/                        ← only if a blueprint is configured
│   ├── 0/result.json                     ← segment 0 custom fields (if MATCH)
│   └── 1/result.json
└── (crop images + per-table CSVs, if additionalFileFormat ENABLED)
```

**`job_metadata.json`** is the map — it contains no results, just pointers:

```jsonc
{
  "job_status": "SUCCESS",
  "output_metadata": [
    {                                       // one entry per input asset (you send 1 file → 1 asset)
      "asset_id": 0,
      "segment_metadata": [                 // one entry per SEGMENT
        {
          "standard_output_path": "s3://.../standard_output/0/result.json",
          "custom_output_path":   "s3://.../custom_output/0/result.json",
          "custom_output_status": "MATCH"   // or "NO_MATCH"
        }
      ]
    }
  ]
}
```

> **Always drive your code off `job_metadata.json`'s paths** — never hardcode `standard_output/0/result.json`. The segment numbering shifts with how the splitter carves each file. (This is exactly what the notebook's `all_segments()` helper does.)

---

## 7. `invoke_data_automation_async` — how it works

The runtime call that **starts** a job. It is **asynchronous**: it returns immediately with a job handle, and processing happens in the background.

### The call

```python
response = bda_runtime.invoke_data_automation_async(
    inputConfiguration={"s3Uri": "s3://bucket/bda/input/doc.pdf"},   # REQUIRED — the file to process (must be in S3)
    outputConfiguration={"s3Uri": "s3://bucket/bda/output"},         # REQUIRED — where results are written
    dataAutomationProfileArn=PROFILE_ARN,                            # REQUIRED — cross-region inference profile (apac./us./eu.)
    dataAutomationConfiguration={                                    # OPTIONAL — which project/recipe to use
        "dataAutomationProjectArn": project_arn,
        "stage": "LIVE"
    },
    # blueprints=[{"blueprintArn": bp_arn, "stage": "LIVE"}],        # OPTIONAL — one-off custom output without a project
    # notificationConfiguration={...},                              # OPTIONAL — EventBridge/SNS notify instead of polling
)
invocation_arn = response["invocationArn"]                          # the job handle
```

Only three params are truly required: **`inputConfiguration`, `outputConfiguration`, `dataAutomationProfileArn`.**

### What happens step by step

1. **Submit** — you call `invoke_data_automation_async`. BDA validates inputs and queues the job.
2. **Return immediately** — you get an `invocationArn`. **No results yet.** The file is *not* processed inline.
3. **Background processing** — BDA reads the file from S3, runs the splitter (if enabled), then the standard and/or custom pipelines per segment. Takes seconds to minutes depending on page count.
4. **Poll for completion** — because it's async, you ask "done yet?" on a loop:

   ```python
   while True:
       r = bda_runtime.get_data_automation_status(invocationArn=invocation_arn)
       status = r["status"]                       # Created → InProgress → Success | ClientError | ServiceError
       if status == "Success": break
       if status in ("ClientError", "ServiceError"):
           raise RuntimeError(r.get("error_message"))
       time.sleep(20)                              # wait, then poll again
   ```

5. **Read results** — on `Success`, `r["outputConfiguration"]["s3Uri"]` points at `job_metadata.json`; follow its paths to each segment's `result.json`.

### Why async (and why polling)?

Document jobs can be large (up to 3,000 pages) and slow — there's no synchronous "submit and get the answer in one call" for this. So the pattern is **submit → poll → read from S3**.

**Production alternative to polling:** pass `notificationConfiguration` so BDA publishes a completion event to EventBridge/SNS, and a Lambda reacts to it — no polling loop, no wasted status calls. Polling is used in the POC only because it's the simplest thing that works in a notebook.

### Two ways to get custom output

- **Project-based** (what §7 does): attach blueprints to a project, invoke with `dataAutomationConfiguration`. Supports the splitter + multi-blueprint **routing**. This is the production path.
- **Ad-hoc**: pass the top-level `blueprints=[...]` parameter directly, no custom project needed. Leanest for a single blueprint on a single file.

---

## 8. End-to-end flow (both pipelines, one file)

```
                       ┌─────────────────────────────────────────────┐
  upload PDF to S3 ──► │  invoke_data_automation_async(project_arn)   │
                       └───────────────────┬─────────────────────────┘
                                           │ returns invocationArn (async)
                                           ▼
                                   ┌────────────────┐
                                   │  poll status   │◄── get_data_automation_status()
                                   └───────┬────────┘
                                           │ Success
                                           ▼
                              read job_metadata.json  (the index)
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    ▼                                              ▼
          per segment: standard_output/n/result.json    per segment: custom_output/n/result.json
          (document / pages / elements /                (matched_blueprint / inference_result /
           tables / figures / summary)                   explainability_info)  — only if MATCH
```

**Bottom line:** the **project** is the recipe, **`invoke...async`** starts the job, the **splitter** turns one file into **segments**, each segment's standard output is organized into **document / pages / elements**, custom output adds your **blueprint fields**, and everything is indexed by **`job_metadata.json`** in your S3 output prefix.
