# Notes on how Bedrock Data Automation actually works

I wrote these up while building the POC notebooks, mostly so I wouldn't have to re-derive all of this the next time. It covers the two ways BDA can process a document, what you get back and where it lands in S3, and the mechanics of the async invoke call. Examples use the `claims-pack.pdf` sample we've been testing with.

One caveat up front: the JSON key names below are from what I saw in practice and from AWS's samples. They're right as far as I can tell, but BDA has been moving fast, so if something doesn't line up, dump your own output with `json.dumps(standard_outputs[0], indent=2)` and trust that over this doc.

## Standard vs custom — two different jobs

BDA does two fundamentally different things, and it's worth being clear on which one you're asking for.

The **standard pipeline** reads the document and gives you back everything that's in it — the text, the tables, any embedded figures, a summary. It doesn't care what kind of document it is; the output has the same shape whether you feed it a claim form or a lease. You configure it through `standardOutputConfiguration`: how granular you want the extraction, which text formats, whether to generate summaries. No blueprint involved. This is what produces the text/tables/figures in the notebook.

The **custom pipeline** does the opposite. Instead of "tell me everything," it's "pull these specific fields." You hand it a blueprint — a JSON schema listing the fields you want (claim number, patient name, total charge, whatever) — and it fills that in. The output is shaped like your schema, not like a generic document. That's §7 in the notebook, with the CMS-1500 claim blueprint.

The nice part is you don't have to pick one. Both configs attach to the same project, so a single job can run standard extraction *and* your blueprint at once. Per document you'll get a standard-output file and, if a blueprint matched, a custom-output file. Roughly:

```python
# standard only — generic extraction, no blueprint
customOutputConfiguration = None

# add custom — also get your named fields
customOutputConfiguration = {"blueprints": [{"blueprintArn": bp_arn, "blueprintStage": "LIVE"}]}
```

The way I think about it: standard reads the document, custom fills in a form you designed. They answer different questions and you usually want both.

Worth knowing the limits differ. Standard scales to 3,000 pages (with the splitter on). Custom blueprints are capped around 20 pages per sub-document and 100 fields, so they run per-piece rather than across a giant file.

## Segments

A segment is one logical sub-document that the splitter pulled out of your file. This is the concept that trips people up, so it's worth slowing down on.

If the splitter is off, the whole file is a single segment, and you're capped at 20 pages — feed it more and the invoke just gets rejected. Turn the splitter on and BDA first classifies the file into separate logical documents, then processes each one on its own. Each piece still has to be 20 pages or under, but the file as a whole can now run up to 3,000. So the 20 is the ceiling on any single unit, and the 3,000 is the ceiling on the whole file once it's been chopped up.

Where the boundaries fall isn't something you control. BDA infers them from the content — a change in document type, a new form header, page numbers restarting, the layout shifting. Your only knob is splitter on or off.

Concretely, `claims-pack.pdf` is 10 pages and might come back as three segments: pages 1–4 as one claim, 5–7 as another, 8–10 as an explanation of benefits. That gives you three standard-output files. With the claim blueprint attached, the two claim segments come back as `MATCH` with fields filled in, and the EOB probably comes back `NO_MATCH` — which is fine, it just isn't a claim form.

One practical thing: each segment records its original page range in `metadata.start_page_index` and `end_page_index`. If you need to cite the real page a value came from, read that — don't trust the per-segment `page_index`, which can restart at zero inside each segment.

## Elements

Inside a single segment's standard output, an element is one structural piece of the document. Every element has a `type`, and there are three you'll actually deal with:

| type | what it is | the fields you want |
|---|---|---|
| `TEXT` | a paragraph or text block | `representation.markdown` / `.text` |
| `TABLE` | a detected table | `representation.csv` / `.html`, `title`, `summary` |
| `FIGURE` | an embedded image, chart, or logo | `crop_images` (S3 URIs of the crop), `summary` (caption) |

Every element also tells you its `reading_order` (where it falls in the flow) and its `locations` — the page index and bounding box of where it physically sits on the page. Elements only show up if you asked for `ELEMENT` granularity; leave that out and you get flowing markdown with no separable tables or figures.

## What standard output looks like

You get one of these files per segment. It's organized into blocks that line up with the granularity levels you requested — document, page, element:

```jsonc
{
  "metadata": {
    "start_page_index": 0,      // where this segment sits in the original file
    "end_page_index": 3,
    "number_of_pages": 4
  },

  "document": {                 // the whole sub-document
    "representation": { "text": "...", "markdown": "...", "html": "..." },
    "statistics": { "element_count": 12, "table_count": 2, "figure_count": 1 },
    "summary": "A paragraph describing this sub-document."
  },

  "pages": [                    // one entry per page — text only, no summary
    { "page_index": 0, "representation": { "markdown": "..." } }
  ],

  "elements": [                 // the tables, figures, and text blocks
    {
      "type": "TABLE",
      "reading_order": 5,
      "locations": [{ "page_index": 1, "bounding_box": { "top": 0.4, "left": 0.1, "width": 0.8, "height": 0.2 } }],
      "representation": { "html": "<table>..</table>", "csv": "col1,col2\n.." },
      "title": "Service Lines",
      "summary": "Table of billed procedures."
    },
    {
      "type": "FIGURE",
      "summary": "Provider logo, top-left.",
      "crop_images": ["s3://.../figure_0.png"]
    }
  ]
}
```

A couple of things that aren't obvious from the structure. The summary lives under `document` — it's one summary for the whole sub-document, not per page. Pages give you text and nothing else. For tables, `representation.csv` is what you load into pandas. For figures, `crop_images` is the actual extracted image (you only get those files if `additionalFileFormat` is enabled), and `summary` is the caption.

## What custom output looks like

This one shows up only for segments where a blueprint matched. It's your fields:

```jsonc
{
  "matched_blueprint": { "name": "poc-health-claim", "confidence": 0.97 },
  "document_class": { "type": "HealthInsuranceClaim" },

  "inference_result": {
    "claim_number": "CLM-0001",
    "patient_name": "Jane Doe",
    "total_charge": 1234.56,
    "balance_due": 234.56,
    "service_lines": [
      { "date_of_service": "2025-01-10", "cpt_code": "99213", "charge": 150.00 }
    ]
  },

  "explainability_info": [
    { "claim_number": { "confidence": 0.98, "geometry": [{ "page": 0, "boundingBox": {} }] } }
  ]
}
```

`inference_result` is what §7 reads and prints. The two fields I'd actually wire into anything real are `explainability_info`, which gives you a per-field confidence score you can threshold on to flag shaky extractions for review, and `matched_blueprint.confidence`, which tells you how sure BDA was about the routing decision itself.

## Where it all ends up in S3

Everything writes under the output prefix you passed to the invoke. There's an index file at the top and then per-segment result files:

```
s3://<bucket>/bda/output/<invocation-id>/
├── job_metadata.json          <- read this first
├── standard_output/
│   ├── 0/result.json
│   ├── 1/result.json
│   └── 2/result.json
└── custom_output/             <- only if a blueprint was configured
    ├── 0/result.json
    └── 1/result.json
```

`job_metadata.json` is the map. It has no results in it, just pointers to the real files and the match status per segment:

```jsonc
{
  "job_status": "SUCCESS",
  "output_metadata": [{
    "asset_id": 0,
    "segment_metadata": [{
      "standard_output_path": "s3://.../standard_output/0/result.json",
      "custom_output_path": "s3://.../custom_output/0/result.json",
      "custom_output_status": "MATCH"
    }]
  }]
}
```

Always walk the paths in `job_metadata.json` rather than hardcoding `standard_output/0/result.json` — the numbering depends on how the splitter carved the file, so it shifts document to document. That's the whole reason the notebook's `all_segments()` helper exists.

## The invoke call and why you have to poll

`invoke_data_automation_async` is what kicks off a job. The "async" in the name is the important part — it doesn't process the file and hand you results, it queues the work and immediately hands you a job ID.

```python
response = bda_runtime.invoke_data_automation_async(
    inputConfiguration={"s3Uri": "s3://bucket/bda/input/doc.pdf"},   # the file (has to be in S3)
    outputConfiguration={"s3Uri": "s3://bucket/bda/output"},         # where results go
    dataAutomationProfileArn=PROFILE_ARN,                            # the cross-region profile (apac./us./eu.)
    dataAutomationConfiguration={                                     # which project to use
        "dataAutomationProjectArn": project_arn,
        "stage": "LIVE"
    },
)
invocation_arn = response["invocationArn"]
```

Only the first three arguments are actually required — input, output, and the profile ARN. The project config is optional (leave it off and you can pass `blueprints=[...]` directly for a one-off custom run).

What happens after you call it: BDA validates the request and queues it, you get back an `invocationArn` with no results attached, and then it processes the file in the background — splitting it if enabled, running the standard and custom pipelines on each segment. That takes anywhere from a few seconds to a few minutes depending on how many pages. Since nothing comes back inline, you check on it in a loop:

```python
while True:
    r = bda_runtime.get_data_automation_status(invocationArn=invocation_arn)
    if r["status"] == "Success":
        break
    if r["status"] in ("ClientError", "ServiceError"):
        raise RuntimeError(r.get("error_message"))
    time.sleep(20)
```

Once it's done, `r["outputConfiguration"]["s3Uri"]` points at `job_metadata.json`, and you follow that to the actual results.

Polling is just the simplest thing that works in a notebook — the reason it exists at all is that these jobs can be big and slow, so there's no synchronous version of the call. In production you'd skip the loop entirely: pass a `notificationConfiguration` on the invoke and BDA fires an event to EventBridge or SNS when the job finishes, and a Lambda picks it up. Same job, no busy-waiting.

## Putting it together

The shape of the whole thing, once it clicks, is fairly simple. A project is the saved recipe — the extraction settings plus any blueprints. `invoke_data_automation_async` starts a job against that recipe. The splitter turns your one file into segments. Each segment's standard output is broken into document / pages / elements, and if a blueprint matched you also get that segment's custom fields. All of it is indexed by `job_metadata.json` sitting in your output prefix, and you read your way out from there.

## PII detection and redaction

BDA can find and mask sensitive data on its own — you don't need to bolt Guardrails on afterward. It's a project-level setting that lives in `overrideConfiguration`, one block per modality (document, image, audio, video). The notebook turns it on for documents.

```python
override_configuration = {
    "document": {
        "splitter": {"state": "ENABLED"},
        "sensitiveDataConfiguration": {
            "detectionMode": "DETECTION_AND_REDACTION",   # or "DETECTION" to only flag
            "detectionScope": ["STANDARD", "CUSTOM"],     # apply to standard and/or custom output
            "piiEntitiesConfiguration": {
                "piiEntityTypes": ["NAME", "EMAIL", "US_SOCIAL_SECURITY_NUMBER"],  # or ["ALL"]
                "redactionMaskMode": "ENTITY_TYPE"        # [NAME], [EMAIL], ...  ("PII" for a generic [PII])
            }
        }
    }
}
```

A few things worth knowing. `detectionMode` is the on/off for redaction versus just flagging — `DETECTION` tells you where the PII is and its confidence but leaves the value in place; `DETECTION_AND_REDACTION` actually masks it in the output. `detectionScope` decides which pipelines get scrubbed — you almost always want both `STANDARD` and `CUSTOM` so a value doesn't leak through one path while you're redacting the other. `redactionMaskMode` is cosmetic but useful: `ENTITY_TYPE` replaces a value with a typed tag like `[NAME]` so you can still see *what* was removed, while `PII` blanks everything to a generic `[PII]`.

The entity list is the same catalog Guardrails supports — names, addresses, phone/email, SSNs, bank and card numbers, passport and license numbers, plus UK and Canada equivalents. You can name up to 32 explicitly or just pass `["ALL"]`. For the claims packet, redaction is genuinely relevant: patient names, SSNs, and member IDs are exactly the fields you don't want sitting in an S3 bucket in plaintext.

Two caveats. This is a newer capability, so you need a recent boto3, and it's worth confirming it's live in your region before relying on it. And redaction is destructive by design — if a downstream step actually needs the real SSN, redact into one copy and keep an access-controlled unredacted copy rather than trying to un-redact later.

## Sync vs async

There are two runtime calls, and they're genuinely different, not just a convenience wrapper.

`invoke_data_automation_async` is the one the notebook uses. You submit, get a job ID back immediately, and poll (or wait on an event) for it to finish. Results are written to S3. This is the only option that handles the big stuff — long documents, the splitter, audio and video — and it's what you'd build a real pipeline on.

`invoke_data_automation` is synchronous. You call it and the results come back in the response, no polling, no S3 round-trip. That's nice for small, interactive, latency-sensitive cases — a single image or a short document where you want the answer inline. The tradeoff is it's meant for small inputs; you can't throw a 500-page PDF at it and wait.

So the rule of thumb: sync for small and interactive, async for anything large, batched, or involving splitting or media. When in doubt, async — it's the general-purpose path, and the polling loop is cheap to write (or you skip it entirely with a notification config).

One thing that's the same either way: price. Sync and async cost the same per unit. You don't pay a premium for async, and you don't get a discount for sync.

## Batch processing

There's no single "batch" API where you hand BDA a folder and walk away — it processes one input file per invoke. But batch in the practical sense (process ten thousand documents) is straightforward, you just orchestrate the invokes yourself. A few common shapes:

The event-driven one is the cleanest for steady volume: drop files in an S3 bucket, have an S3 event trigger a Lambda, and the Lambda calls `invoke_data_automation_async` with a notification config. Completions come back as EventBridge or SNS events that a second Lambda handles. No polling, no servers sitting idle, and it scales with whatever lands in the bucket.

For a defined set of files you want to run once, a loop or a Step Functions map over the list works fine — kick off all the async jobs, then collect results as they finish. And if BDA is just the parsing step feeding a RAG system, the Knowledge Bases integration handles the batching for you: point a Knowledge Base at an S3 prefix with BDA as the parser and it processes everything on ingestion.

Worth flagging: "batch" here doesn't mean the discounted batch-inference tier that Bedrock offers for foundation models. That 50%-off batch mode is a token-model thing. BDA is per-unit priced and there's no volume discount for running many files — a thousand documents costs a thousand documents whether you send them one at a time or all at once. Batching buys you throughput and simpler orchestration, not a lower rate. Do watch your account's concurrent-job quota, though; that's the real limit when you fan out.

## What it costs

BDA prices per unit of input — pages, images, minutes — not per token, which makes it easy to forecast. Standard output and custom output are billed differently, and custom scales with how many fields your blueprint has. These are the published rates (verify against the pricing page and confirm the ap-south-1 numbers, but they've been stable):

| Input | Standard output | Custom output (blueprint) |
|---|---|---|
| Document | $0.010 / page | $0.040 / page for ≤30 fields; +$0.0005 per field above 30 |
| Image | per image (see pricing page) | $0.005 / image for ≤30 fields; +$0.0005 per field above 30 |
| Audio | $0.006 / minute | — |
| Video | $0.050 / minute | (video blueprints priced per minute) |

The field-count tier is the thing to internalize for custom output. Up to 30 fields you pay the flat per-page (or per-image) rate; every field beyond 30 adds half a tenth of a cent. So a 40-field document blueprint is $0.045/page rather than $0.040 — the claim blueprint in the notebook has well under 30 fields, so it sits at the base rate.

Some worked numbers, straight from AWS's examples. A 1,000-page document through custom output with a 15-field blueprint is 1,000 × $0.040 = $40. The same 1,000 pages through standard output (the mode the Knowledge Bases integration uses) is 1,000 × $0.010 = $10. A 60-minute video in standard output is 60 × $0.050 = $3.00. And 15,000 minutes of meeting audio is 15,000 × $0.006 = $90.

For the notebook specifically: running `claims-pack.pdf` (10 pages) through standard output is about 10¢, and the §7 custom pass adds roughly another 40¢ for those pages — call it 50¢ per full run of the notebook, since §7 re-invokes the whole document. That re-invoke is the one cost gotcha: because §7 runs a second job to add custom output, you pay for two passes over the pages, not one. If you wanted to avoid that in production you'd configure standard + custom on the project once and invoke a single time.

## What BDA does per modality

The four input types get genuinely different treatment. Here's what each one gives you.

**Documents** are the deepest. Standard output does full text extraction with layout and reading order, markdown/HTML/CSV representations, tables pulled out as structured data, embedded figures cropped and captioned, and document/page summaries. The splitter classifies multi-document packets and routes them. Custom blueprints pull named fields. This is intelligent document processing end to end — classify, extract, normalize, validate — without you stitching those steps together. Up to 3,000 pages with the splitter.

**Images** give you a summary of the image, text detection (OCR of text baked into the image), logo detection, IAB content categorization, and content moderation flags. Custom image blueprints let you define fields to pull from a picture — think ID cards, product shots, screenshots. AWS shipped a roughly 50% speedup on image processing in late 2025.

**Audio** gives you a transcript with speaker diarization, an overall summary, chapter/segment-level summaries, and content moderation. Custom is more limited here than for documents — the value is mostly in the rich standard transcript-plus-insights. At $0.006/minute it's the cheapest modality by far.

**Video** is the richest media type. Standard output does scene-by-scene descriptions, shot detection and chapter segmentation, text and logo detection, IAB taxonomy, content moderation, and a transcript of the spoken audio track. Video blueprints (added in 2025) let you customize what to generate per scene — summaries, content tags, object detection — which is aimed at media search, highlight generation, and contextual ad placement. Format support was broadened to include AVI, MKV, and WEBM alongside the usual MP4/MOV.

The common thread: every modality has a standard mode that gives you a generically-useful structured output with zero setup, and most have a custom mode where a blueprint tailors the output to your schema. Documents and video have the most developed custom story; audio leans on its standard output.

## Tuning a blueprint with instruction optimization

Once you've built a blueprint, the part that actually determines accuracy is the per-field `instruction` text — those little natural-language hints like "ICD-10 diagnosis codes, comma-separated." Getting them right is normally trial and error: run the blueprint, eyeball what came out wrong, reword an instruction, run again. `InvokeBlueprintOptimizationAsync` automates exactly that loop, and it's worth knowing about because it turns days of hand-tuning into a job that finishes in minutes.

The idea is supervised, not magic. You hand BDA your existing blueprint plus a small set of example documents — three to ten is the sweet spot — where each example comes with the *correct* answer, the ground truth. BDA runs its own extraction on those documents, compares what it got against your ground truth field by field, and then rewrites the natural-language instructions to close the gaps it found. Nothing gets fine-tuned at the model level; it's the instructions that change. So if your invoices from one vendor label the total as "Amount Due" and another calls it "Balance," the optimizer notices your ground truth expects both to map to `total_charge` and adjusts the instruction to catch both phrasings.

Each sample is a pair — the document and its labels:

```python
samples = [
    {
        "assetS3Object":       {"s3Uri": "s3://bucket/samples/claim_01.pdf"},
        "groundTruthS3Object": {"s3Uri": "s3://bucket/samples/claim_01_truth.json"},
    },
    # ... 3 to 10 of these
]
```

The API flow mirrors the invoke pattern — it's async, so you kick it off and poll:

```python
job = bda_client.invoke_blueprint_optimization_async(
    blueprint={"blueprintArn": bp_arn, "blueprintStage": "LIVE"},
    samples=samples,
    outputConfiguration={"s3Uri": "s3://bucket/optimization-output"},
    dataAutomationProfileArn=PROFILE_ARN,
)
arn = job["invocationArn"]

# poll until done
r = bda_client.get_blueprint_optimization_status(invocationArn=arn)   # -> status + outputConfiguration
# then read the optimized blueprint back with get_blueprint(...), and
# copy_blueprint_stage(...) to promote it from DEVELOPMENT to LIVE
```

What you get back is the genuinely useful part: alongside the refined blueprint, BDA reports evaluation metrics measured against your ground truth — an exact-match rate and an F1 score per field. That's the thing that gives you a real "is this production-ready" signal instead of a gut feeling. In AWS's own purchase-order example the aggregate exact match moved from 90% to 92% after optimization — modest-sounding, but at volume a couple of points off your error rate is a meaningful chunk of the manual-review queue gone.

Why it's worth reaching for: it replaces the slowest, most tedious part of building a good blueprint. Instead of guessing at instruction wording and re-running by hand, you spend your effort once on labeling a handful of representative documents — and labeling is easy, the console even has an auto-populate that runs a first inference pass so you only correct the values it got wrong. It needs no ML expertise and no training data beyond those few examples, and because it only rewrites instructions it doesn't touch your schema or your field set. The main gotcha to remember: if you later add or remove fields, the optimization history is discarded and you re-optimize, so download the manifest of your samples and ground truth before editing a tuned blueprint.

For a real ScoreNLearn pipeline this is the natural next step after the POC. Build the transcript or score-report blueprint, hand-label five or six real documents, run optimization, and check the F1 before trusting it on the full pipeline — that's how you get from "works on the sample" to "works on the messy variety of documents students actually send."
