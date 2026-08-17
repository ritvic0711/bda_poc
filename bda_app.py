"""
BDA Explorer — a Streamlit UI for Amazon Bedrock Data Automation.

Upload a document or image, run it through BDA end to end, and browse the results:
document summary, full text, tables (as DataFrames), figures (extracted images),
custom blueprint fields, RAG chunks, and the raw result JSON.

RUN IT:
    pip install streamlit boto3 pandas
    streamlit run bda_app.py

The machine you run it on needs AWS credentials (env vars, ~/.aws, or an instance/role)
with BDA + S3 permissions, in a region where BDA is available (e.g. ap-south-1).
"""

import streamlit as st
import boto3, json, io, time
from urllib.parse import urlparse
import pandas as pd

st.set_page_config(page_title="BDA Explorer", layout="wide", page_icon="📄")

# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def split_s3(uri):                                     # "s3://bucket/key" -> ("bucket", "key")
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")

def cris_prefix(region):                               # cross-region inference geography for the profile ARN
    if region.startswith("us-") or region.startswith("us_gov"): return "us"
    if region.startswith("eu-"): return "eu"
    if region.startswith("ap-"): return "apac"
    raise ValueError(f"Unknown geography for region {region}")

@st.cache_resource(show_spinner=False)                 # build clients once per region (cached across reruns)
def get_ctx(region):
    sess = boto3.Session(region_name=region)
    ctx = {
        "s3":  sess.client("s3"),
        "sts": sess.client("sts"),
        "bda": sess.client("bedrock-data-automation"),          # build-time: blueprints + projects
        "rt":  sess.client("bedrock-data-automation-runtime"),  # runtime: invoke + status
    }
    ctx["account"] = ctx["sts"].get_caller_identity()["Account"]
    return ctx

def ensure_bucket(s3, name, region):                   # create the bucket if it doesn't exist
    try:
        s3.head_bucket(Bucket=name); return
    except Exception:
        pass
    kw = {"Bucket": name}
    if region != "us-east-1":
        kw["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kw)

def read_s3_json(s3, uri):
    b, k = split_s3(uri); return json.loads(s3.get_object(Bucket=b, Key=k)["Body"].read())

def read_s3_bytes(s3, uri):
    b, k = split_s3(uri); return s3.get_object(Bucket=b, Key=k)["Body"].read()

# A default custom blueprint (medical claim) users can edit or replace in the sidebar.
DEFAULT_CLAIM_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "description": "US health insurance claim (CMS-1500 style): patient, insured, provider, billing.",
    "class": "HealthInsuranceClaim",
    "type": "object",
    "definitions": {
        "ServiceLine": {"properties": {
            "date_of_service": {"type": "string", "inferenceType": "explicit", "instruction": "Service line date, YYYY-MM-DD"},
            "cpt_code":        {"type": "string", "inferenceType": "explicit", "instruction": "CPT / HCPCS procedure code"},
            "description":     {"type": "string", "inferenceType": "explicit", "instruction": "Procedure description"},
            "charge":          {"type": "number", "inferenceType": "explicit", "instruction": "Charge for this line"}}}
    },
    "properties": {
        "claim_number":    {"type": "string", "inferenceType": "explicit", "instruction": "Claim or patient control number"},
        "patient_name":    {"type": "string", "inferenceType": "explicit", "instruction": "Full name of the patient"},
        "insured_id":      {"type": "string", "inferenceType": "explicit", "instruction": "Insured member / policy ID number"},
        "provider_name":   {"type": "string", "inferenceType": "explicit", "instruction": "Billing provider or facility name"},
        "date_of_service": {"type": "string", "inferenceType": "explicit", "instruction": "Date(s) of service, YYYY-MM-DD"},
        "diagnosis_codes": {"type": "string", "inferenceType": "explicit", "instruction": "ICD-10 diagnosis codes, comma-separated"},
        "total_charge":    {"type": "number", "inferenceType": "explicit", "instruction": "Total charge / billed amount"},
        "service_lines":   {"type": "array", "instruction": "Every procedure/service line on the claim",
                             "items": {"$ref": "#/definitions/ServiceLine"}}
    }
}

# ----------------------------------------------------------------------------
# Config builders
# ----------------------------------------------------------------------------
def build_standard_config():                           # what standard extraction to produce
    return {"document": {
        "extraction": {"granularity": {"types": ["DOCUMENT", "PAGE", "ELEMENT"]},
                        "boundingBox": {"state": "ENABLED"}},
        "generativeField": {"state": "ENABLED"},
        "outputFormat": {"textFormat": {"types": ["PLAIN_TEXT", "MARKDOWN", "HTML", "CSV"]},
                         "additionalFileFormat": {"state": "ENABLED"}}}}

def build_override(splitter, redact_pii, pii_types):   # splitter + optional PII redaction
    doc = {"splitter": {"state": "ENABLED" if splitter else "DISABLED"}}
    if redact_pii:
        doc["sensitiveDataConfiguration"] = {
            "detectionMode": "DETECTION_AND_REDACTION",
            "detectionScope": ["STANDARD", "CUSTOM"],
            "piiEntitiesConfiguration": {"piiEntityTypes": pii_types, "redactionMaskMode": "ENTITY_TYPE"}}
    return {"document": doc}

def upsert_blueprint(bda, name, schema):               # create or update a blueprint by name -> ARN
    existing = next((b for b in bda.list_blueprints(blueprintStageFilter="ALL").get("blueprints", [])
                     if b.get("blueprintName") == name), None)
    if existing:
        return bda.update_blueprint(blueprintArn=existing["blueprintArn"], blueprintStage="LIVE",
                                    schema=json.dumps(schema))["blueprint"]["blueprintArn"]
    return bda.create_blueprint(blueprintName=name, type="DOCUMENT", blueprintStage="LIVE",
                                schema=json.dumps(schema))["blueprint"]["blueprintArn"]

def upsert_project(bda, name, std_cfg, override, blueprint_arn=None):   # create/update the project -> ARN
    kwargs = dict(standardOutputConfiguration=std_cfg, overrideConfiguration=override)
    if blueprint_arn:
        kwargs["customOutputConfiguration"] = {"blueprints": [{"blueprintArn": blueprint_arn, "blueprintStage": "LIVE"}]}
    existing = next((p for p in bda.list_data_automation_projects(projectStageFilter="LIVE").get("projects", [])
                     if p["projectName"] == name), None)
    if existing:
        return bda.update_data_automation_project(projectArn=existing["projectArn"], **kwargs)["projectArn"]
    return bda.create_data_automation_project(projectName=name, projectStage="LIVE",
                                              projectDescription="Streamlit BDA explorer", **kwargs)["projectArn"]

# ----------------------------------------------------------------------------
# The actual BDA run (returns everything the UI needs, stored in session_state)
# ----------------------------------------------------------------------------
def run_bda(ctx, bucket, region, file_bytes, file_name, cfg, status_box):
    s3, bda, rt = ctx["s3"], ctx["bda"], ctx["rt"]
    profile_arn = f"arn:aws:bedrock:{region}:{ctx['account']}:data-automation-profile/{cris_prefix(region)}.data-automation-v1"
    s3_in  = f"s3://{bucket}/bda-app/input/{file_name}"
    s3_out = f"s3://{bucket}/bda-app/output"

    status_box.write("Uploading file to S3…")
    b, k = split_s3(s3_in); s3.put_object(Bucket=b, Key=k, Body=file_bytes)

    status_box.write("Configuring project…")
    std_cfg  = build_standard_config()
    override = build_override(cfg["splitter"], cfg["redact_pii"], cfg["pii_types"])
    bp_arn = upsert_blueprint(bda, "bda-app-blueprint", cfg["schema"]) if cfg["custom"] else None
    project_arn = upsert_project(bda, "bda-app-project", std_cfg, override, bp_arn)

    status_box.write("Starting BDA job…")
    inv = rt.invoke_data_automation_async(
        inputConfiguration={"s3Uri": s3_in},
        outputConfiguration={"s3Uri": s3_out},
        dataAutomationProfileArn=profile_arn,
        dataAutomationConfiguration={"dataAutomationProjectArn": project_arn, "stage": "LIVE"},
    )["invocationArn"]

    # poll
    for _ in range(80):
        r = rt.get_data_automation_status(invocationArn=inv)
        stt = r["status"]
        status_box.write(f"Job status: **{stt}**")
        if stt == "Success": break
        if stt in ("ClientError", "ServiceError"):
            raise RuntimeError(r.get("error_message") or stt)
        time.sleep(15)
    else:
        raise TimeoutError("Job did not finish in time.")

    status_box.write("Fetching results…")
    meta = read_s3_json(s3, r["outputConfiguration"]["s3Uri"])
    standard_outputs, custom_outputs = [], []
    for asset in meta.get("output_metadata", []):
        for seg in asset.get("segment_metadata", []):
            if seg.get("standard_output_path"):
                standard_outputs.append(read_s3_json(s3, seg["standard_output_path"]))
            custom_outputs.append(
                read_s3_json(s3, seg["custom_output_path"])
                if seg.get("custom_output_status") == "MATCH" and seg.get("custom_output_path") else None)

    # derive text, tables, figures, chunks
    summaries, pages_md = [], []
    tables, figures, chunks = [], [], []
    for seg_i, std in enumerate(standard_outputs):
        summaries.append((std.get("document", {}) or {}).get("summary"))
        for pg in std.get("pages", []):
            pages_md.append((pg.get("representation", {}) or {}).get("markdown", ""))
        for el in std.get("elements", []):
            loc = (el.get("locations") or [{}])[0]
            page = loc.get("page_index", (el.get("page_indices") or [None])[0])
            rep = el.get("representation", {}) or {}
            et = el.get("type")
            if et == "TABLE":
                df = None
                if rep.get("csv"):
                    try: df = pd.read_csv(io.StringIO(rep["csv"]))
                    except Exception: df = None
                if df is None and rep.get("html"):
                    try: df = pd.read_html(io.StringIO(rep["html"]))[0]
                    except Exception: df = None
                tables.append({"page": page, "title": el.get("title"), "summary": el.get("summary"), "df": df})
                content = f"[TABLE] {el.get('title') or ''}\n{rep.get('markdown') or rep.get('csv') or ''}"
            elif et == "FIGURE":
                for crop in (el.get("crop_images") or []):
                    try: figures.append({"page": page, "caption": el.get("summary") or "", "bytes": read_s3_bytes(s3, crop)})
                    except Exception: pass
                content = f"[FIGURE] {el.get('summary') or '(image)'}"
            else:
                content = rep.get("markdown") or rep.get("text") or ""
            if content.strip():
                chunks.append({"type": et, "page": page, "segment": seg_i, "content": content})

    return {
        "n_segments": len(standard_outputs),
        "summaries": summaries,
        "full_text": "\n\n".join(m for m in pages_md if m),
        "tables": tables,
        "figures": figures,
        "chunks": chunks,
        "custom": custom_outputs,
        "standard_outputs": standard_outputs,
        "job_metadata": meta,
    }

# ----------------------------------------------------------------------------
# Sidebar — configuration
# ----------------------------------------------------------------------------
st.sidebar.header("⚙️ Configuration")
region = st.sidebar.text_input("AWS region", value="ap-south-1",
                               help="A region where BDA is available (e.g. ap-south-1, us-east-1, us-west-2).")
bucket_in = st.sidebar.text_input("S3 bucket (blank = default)", value="",
                                  help="Leave blank to use sagemaker-<region>-<account> (created if missing).")

st.sidebar.subheader("Processing")
splitter   = st.sidebar.checkbox("Enable splitter (long / multi-doc files)", value=True)
redact_pii = st.sidebar.checkbox("Detect + redact PII", value=False)
pii_types = st.sidebar.multiselect(
    "PII types to redact",
    ["ALL", "NAME", "ADDRESS", "PHONE", "EMAIL", "US_SOCIAL_SECURITY_NUMBER",
     "US_BANK_ACCOUNT_NUMBER", "CREDIT_DEBIT_CARD_NUMBER"],
    default=["NAME", "US_SOCIAL_SECURITY_NUMBER"], disabled=not redact_pii)

st.sidebar.subheader("Custom blueprint")
custom = st.sidebar.checkbox("Extract custom fields (blueprint)", value=False)
schema_text = st.sidebar.text_area("Blueprint schema (JSON)", value=json.dumps(DEFAULT_CLAIM_SCHEMA, indent=2),
                                   height=260, disabled=not custom,
                                   help="Edit to match your document type. List fields go inside 'properties'.")

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
st.title("📄 Bedrock Data Automation — Explorer")
st.caption("Upload a document or image, run it through BDA, and browse the extracted results.")

uploaded = st.file_uploader("Upload a file", type=["pdf", "png", "jpg", "jpeg", "tiff"])

if st.button("▶️ Run BDA", type="primary", disabled=uploaded is None):
    # validate the schema JSON up front
    schema_obj = None
    if custom:
        try:
            schema_obj = json.loads(schema_text)
        except Exception as e:
            st.error(f"Blueprint schema is not valid JSON: {e}"); st.stop()

    ctx = get_ctx(region)
    bucket = bucket_in.strip() or f"sagemaker-{region}-{ctx['account']}"
    try:
        ensure_bucket(ctx["s3"], bucket, region)
    except Exception as e:
        st.error(f"Could not access/create bucket '{bucket}': {e}"); st.stop()

    cfg = {"splitter": splitter, "redact_pii": redact_pii,
           "pii_types": pii_types or ["NAME"], "custom": custom, "schema": schema_obj}

    with st.status("Running BDA…", expanded=True) as status:
        try:
            st.session_state["results"] = run_bda(
                ctx, bucket, region, uploaded.getvalue(), uploaded.name, cfg, status)
            status.update(label="Done ✅", state="complete")
        except Exception as e:
            status.update(label="Failed ❌", state="error")
            st.exception(e); st.stop()

# ----------------------------------------------------------------------------
# Render results (from session_state, so switching tabs doesn't re-run the job)
# ----------------------------------------------------------------------------
res = st.session_state.get("results")
if res:
    st.success(f"Processed into {res['n_segments']} segment(s).")
    tabs = st.tabs(["📝 Summary", "📄 Text", "📊 Tables", "🖼️ Figures", "🧾 Custom fields", "🧩 Chunks", "🗄️ Raw JSON"])

    with tabs[0]:
        for i, s in enumerate(res["summaries"]):
            st.markdown(f"**Segment {i}**")
            st.markdown(s or "_no summary_")

    with tabs[1]:
        st.markdown(res["full_text"][:20000] or "_no text_")
        st.download_button("Download full text (.md)", res["full_text"], "full_text.md")

    with tabs[2]:
        if not res["tables"]:
            st.info("No tables detected.")
        for i, t in enumerate(res["tables"]):
            st.markdown(f"**p{t['page']} · {t['title'] or f'table {i}'}**" + (f" — _{t['summary']}_" if t['summary'] else ""))
            if t["df"] is not None:
                st.dataframe(t["df"], use_container_width=True)
            else:
                st.caption("(could not parse this table)")

    with tabs[3]:
        if not res["figures"]:
            st.info("No figures detected (needs additionalFileFormat + figures in the doc).")
        cols = st.columns(3)
        for i, f in enumerate(res["figures"]):
            with cols[i % 3]:
                st.image(f["bytes"], caption=f"p{f['page']} — {f['caption'] or ''}", use_container_width=True)

    with tabs[4]:
        matched = [(i, c) for i, c in enumerate(res["custom"]) if c]
        if not matched:
            st.info("No custom output. Enable the blueprint in the sidebar and re-run, or no segment matched.")
        for seg_i, c in matched:
            fields = c.get("inference_result") or c
            st.markdown(f"**Segment {seg_i} — MATCH**")
            scalars = {k: v for k, v in fields.items() if not isinstance(v, (list, dict))}
            if scalars:
                st.dataframe(pd.DataFrame(scalars.items(), columns=["field", "value"]), use_container_width=True)
            for k, v in fields.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    st.caption(k); st.dataframe(pd.DataFrame(v), use_container_width=True)

    with tabs[5]:
        st.write(f"{len(res['chunks'])} element-aware chunks")
        counts = pd.Series([c["type"] for c in res["chunks"]]).value_counts().rename_axis("type").reset_index(name="count")
        st.dataframe(counts, use_container_width=True)
        st.download_button("Download chunks (.json)", json.dumps(res["chunks"], indent=2, default=str), "chunks.json")

    with tabs[6]:
        st.download_button("Download job_metadata.json", json.dumps(res["job_metadata"], indent=2, default=str), "job_metadata.json")
        for i, std in enumerate(res["standard_outputs"]):
            with st.expander(f"standard_output — segment {i}"):
                st.json(std, expanded=False)
        for i, c in enumerate(res["custom"]):
            if c:
                with st.expander(f"custom_output — segment {i}"):
                    st.json(c, expanded=False)
