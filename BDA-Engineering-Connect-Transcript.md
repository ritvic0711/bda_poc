# AskGartner on BDA — Speaking Transcript

**Weekly Engineering Connect · ~18–20 minutes · 13 slides**
Shape: ~5 BDA foundation slides, then ~7 on the AskGartner design, then takeaways. Target ~75–95 sec/slide; AskGartner slides (7–11) run longer. `[...]` = pause.

---

## Slide 1 — Cover: AskGartner on Bedrock Data Automation
*(~45 sec)*

POC walkthrough, not a sales pitch. The short version: **AskGartner** lets a user upload their own documents, ask a question, and get an answer grounded in both Gartner research *and* the content of what they uploaded.

Bedrock Data Automation — **BDA** — is the engine that reads those documents. I'll spend the first few slides on just enough BDA to make the design legible, then the bulk of the time on the AskGartner architecture itself. About eighteen minutes, then questions.

---

## Slide 2 — What BDA is (the engine, one slide)
*(~70 sec)*

One slide on the engine, then we move to the real subject.

BDA is a *managed* service — you stop assembling the extraction stack yourself. One API across documents, images, audio and video. It does the unglamorous work: OCR, model choice, prompting. Every extracted value comes back with a **confidence score** and page coordinates, which is what lets us gate on quality.

And critically for us: its output is shaped to drop straight into Knowledge Bases and Agents — which is exactly the downstream AskGartner runs on. [...]

---

## Slide 3 — Standard vs. custom, and segments
*(~80 sec)*

Two things to know about how BDA works.

First, it does two jobs. **Standard** is "tell me everything" — text, tables, figures, same shape for any document. **Custom** is "pull these fields" — you hand it a JSON blueprint and output matches your schema. Both run in one job. For AskGartner we lean on **standard** — we want everything in the document as clean markdown.

Second, the unit of work is a **segment**: a big file is split into logical sub-documents, twenty pages each, up to three thousand across the file. BDA infers the boundaries; your only knob is splitter on/off.

One gotcha worth carrying: to cite the true page a value came from, read `start_page_index` — the per-segment index restarts at zero. [...]

---

## Slide 4 — What comes back: markdown + confidence
*(~75 sec)*

What BDA hands back. Standard output is one file per segment: a `document` block with the whole thing as markdown, a `pages` array, and — if you ask for element granularity — the individual tables, figures and text blocks with their reading order and location.

Two takeaways. The **markdown representation** is the thing the AskGartner agent actually reads — reading-order text, tables, figure captions, one LLM-ready blob per sub-document. And the **confidence scores** are the real product: custom output attaches a per-field confidence you threshold on, so low-confidence fields go to review and the confident ones auto-approve. That's the quality gate. [...]

---

## Slide 5 — Runtime & governance (why it matters for us)
*(~80 sec)*

Two runtime facts the AskGartner design leans on directly.

First, BDA is **async**: you submit, get a job ID, results land in S3 — and in production you don't poll, you pass a notification config and get an EventBridge or SNS event when it's done. That's what lets extraction run *out-of-band* from the conversation loop.

Second, **PII detection and redaction is native** — a project-level config scrubs names, SSNs, bank and card numbers across both pipelines. One caveat: redaction is destructive, so keep an access-controlled unredacted copy if a downstream step needs the real value.

The point of the slide is the dark strip: async-plus-events and native PII are the two properties the AskGartner architecture is built on. Now let's get to that architecture. [...]

---

## Slide 6 — AskGartner: the product & the goal
*(~75 sec)*

This is the thing we're building, and it's the heart of the talk.

AskGartner is a user-facing experience: you upload one or more files, you ask a question, and you get an agent-generated answer that draws on **both** your own uploaded documents **and** Gartner's published research corpus. That "and" is the whole product — it bridges external, client-owned context with Gartner's research.

The way the design doc frames it: it turns the traditionally slow and rigid document-review process into a scalable, near-instant *conversational* review workflow.

Everything from here is how we make that real — the components, the data flows, how it fails safely, how data is governed, and how it survives a region outage. [...]

---

## Slide 7 — Target architecture: the backbone
*(~100 sec)*

This is the backbone. Walk the top row left to right: the UI captures the file and the question; the **ingestion API** hands back a presigned S3 URL so the browser streams the binary straight to S3 — nothing in compute ever holds the raw bytes; the **security gate** runs GuardDuty, magic-byte and password-protection checks; the file lands in S3; **BDA extracts**.

Bottom row, right to left: BDA writes markdown to the output bucket, a **normalizer** chunks it to the token budget, and the **AskGartner agent on AgentCore** retrieves that context and grounds the answer with Convologic plus Gartner research.

The four components underneath are the ones worth naming: the ingestion gateway that mints presigned URLs and tracks file state; the security gate; the normalizer that turns raw BDA JSON into chunked markdown; and the AgentCore context tool that exposes MCP tools so the agent pulls context by actor, session and file id. Hold those names — the data flows next make them concrete. [...]

---

## Slide 8 — Core data flows: upload, processing, answer
*(~100 sec)*

The design doc breaks it into three paths, and this is the operational heart.

**Upload path:** the client sends metadata first, the API validates and returns a presigned URL, the browser streams straight to S3, and on landing every security check runs — nothing skipped for latency — with PPT and PPTX converted to PDF after the gate.

**Processing path:** the validated file is dispatched async to BDA, roughly a ten-second P95 extraction fully isolated from the conversation loop; BDA pulls reading-order text, tables and visual elements, with OCR on scanned pages; the markdown lands in chat history and each file shows as its own chip.

**Answer path:** submit is disabled until every attachment resolves or is removed — so you never ask a question against a half-processed file; then Convologic retrieves the markdown and applies Gartner grounding. One important scoping rule: document awareness persists only within the same thread. [...]

---

## Slide 9 — Answer quality & resilience
*(~95 sec)*

Two things that make AskGartner production-grade rather than a demo.

On the left, **grounding tiers** decide what kind of answer you get. T1 is a full structured review with sources — strong Gartner coverage plus the user's document. T2 is where research coverage is thin or absent — it still answers, but flagged. T3 is an unsupported use case, and instead of hallucinating it **routes to a human analyst**, carrying the session context and the file attachments through to the existing inquiry workstream — so the user doesn't start over.

On the right, the **failure model**: anything invalid, oversized, over the two-file limit, mismatched, password-protected or corrupt is rejected immediately with a clear error; GuardDuty blocks infected files; extraction past twice the target latency is marked failed so the user re-uploads instead of staring at a spinner; and if extraction fails *after* the question is in flight, the conversation recovers — it answers without the attachment or asks the user to re-attach.

The theme: it fails loudly and never hangs the thread. [...]

---

## Slide 10 — Data management & governance
*(~90 sec)*

This is the slide the security-minded people in the room will care about.

The table is the data lifecycle: the raw binary lands in an S3 raw bucket keyed by session and file, thread-scoped TTL; the intermediate BDA JSON goes to an extracted bucket and is purged right after normalization; the parsed markdown lives in chat history, retained only for the active thread; and a Postgres tracking record keyed by session and file follows the session TTL.

The through-line is at the bottom: every artifact is partitioned strictly by `session_id` and `actor_id` — no cross-session persistence and no multi-session document libraries in v1. Uploads are treated as ephemeral input context only; the system deliberately does *not* classify privilege or confidentiality — that stays client-managed. And telemetry never logs raw file contents or PII.

Session-scoped, ephemeral, and isolated by design. [...]

---

## Slide 11 — Reliability & observability
*(~95 sec)*

The production-readiness slide.

On **DR**: the Document Ingestion Service runs active in both us-east-1 and us-west-2. Users upload to the regional S3 landing bucket, and the raw and output buckets stay synchronized across regions with bi-directional cross-region replication. The active region invokes BDA and writes to its regional output, and Convologic reads from that active source. Two design rules carry here: ingestion and workers are stateless so they absorb a slow extraction without blocking the conversation path, and the malware verdict and extraction stay separate steps — we never parse before the safety verdict is in.

On **observability**: we track document-ready latency across every stage, throughput and queue depth and worker saturation, success and failure by stage, extraction quality, the confidence scores for grounding readiness, and cost per processed file — all on CloudWatch, Splunk and Dynatrace with alerts for stuck jobs, dead-letter-queue growth and cost anomalies.

Fully instrumented, end to end. [...]

---

## Slide 12 — Why BDA, for this
*(~80 sec)*

Pulling it together: why BDA is the right engine for *this* product specifically.

It's multimodal through one integration — strategy decks, architecture diagrams, scanned PDFs, images, the messy artifacts clients actually upload. It's out-of-band by design. It returns markdown the agent can ground on directly, so there's no custom parser to maintain. Its confidence scores tell us when an extraction is trustworthy enough to answer on versus flag or route — that feeds the grounding tiers. PII is native. And it's managed — no OCR stack, no model routing, no fine-tuning to own; Terraform provisions it and the team spends its time on product.

That's the fit. [...]

---

## Slide 13 — Takeaways
*(~60 sec + discussion)*

Four things to carry out:

1. **AskGartner = Gartner research + your own documents.** BDA is the engine that reads the documents; the product is the grounded answer. Don't conflate them.
2. **Extraction runs async and out-of-band** — the conversation never blocks on a slow parse, and security always clears before parsing.
3. **Everything is session-scoped and ephemeral** — isolation by actor/session/file, no cross-session persistence in v1.
4. **It fails loudly and degrades by tier** — T3 routes to a human analyst with context intact. Multi-region active, fully instrumented.

The open question I'd like us to chew on: beyond AskGartner, **what's the next document-heavy flow in our stack worth pointing this same pipeline at?**

Let's open it up. [...]

---

### Delivery notes
- **Runtime:** ~18–20 min of content at this density, leaving a few minutes for Q&A inside a 20-minute slot. If you're tight, compress slides 2–4 (the BDA foundation) — the room can take those faster.
- **Pacing:** slides 1–5 are setup — keep them moving. Slow down on 7 (the diagram) and 8–11 (the AskGartner substance); that's what this audience came for.
- **The money slide is 6** — the goal line, "Gartner research AND the user's own documents." If anyone zones out, bring them back there.
- **If asked about cost:** Documents $0.010/page standard, $0.040 custom; Images $0.003 / $0.005; Audio $0.006/min / $0.009/min; Video $0.050/min / $0.084/min. Custom blueprints >30 fields add $0.0005 per extra field.
- **If asked "why not build the extraction ourselves":** slide 12 is the answer — you'd be hand-rolling OCR, model routing, the markdown normalizer, and the PII layer, and owning all of it.
