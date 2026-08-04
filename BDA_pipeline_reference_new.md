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
