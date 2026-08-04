# Comprehensive Executive Briefing: Amazon Bedrock Data Automation (BDA)

**Target Audience:** Technical Leadership / Executive Team
**Topic:** Scalable Multimodal Data Extraction and Processing via Amazon Bedrock

---

## 1. Executive Summary
Amazon Bedrock Data Automation (BDA) is a fully managed, multimodal GenAI service designed to seamlessly convert complex, unstructured data (documents, images, video, audio) into highly structured, actionable insights. By grouping configurations into Data Projects, BDA eliminates the need for custom chunking, manual orchestration, and brittle OCR pipelines. It enables enterprise-scale automation with built-in security, PII redaction, and compliance controls.

---

## 2. Granular Cost Structure
BDA operates on a consumption-based pricing model mapped directly to the specific data modality being processed.

| Modality | Pricing | Notes |
| :--- | :--- | :--- |
| **Documents** | $0.010 per page | Flat rate for parsing and extracting data from standard documents (PDF, DOC, DOCX). |
| **Images** | $0.0003 per image | Extracted insights, OCR, and visual grounding for visual assets (JPEG, PNG). |
| **Audio** | $0.006 per minute | Speech transcription and structured insight extraction. |
| **Video** | $0.050 per minute | Multimodal frame and audio track analysis. |

*Additional costs:* If integrated with Bedrock Agents or Knowledge Bases, standard Generative AI inference token costs will apply based on the selected foundation model (e.g., Claude, Amazon Nova). Data storage costs for inputs, outputs, and audit logs are billed at standard AWS S3 rates.

*Sources: [CloudZero - Amazon Bedrock Pricing](https://www.cloudzero.com/blog/amazon-bedrock-pricing/)*

---

## 3. Execution Models: Synchronous vs. Asynchronous Processing

BDA supports both real-time and batch-optimized processing patterns to suit diverse application requirements. **Batch processing is natively supported via the Asynchronous model.**

| Feature | `InvokeDataAutomation` (Synchronous) | `InvokeDataAutomationAsync` (Asynchronous) |
| :--- | :--- | :--- |
| **Primary Use Case** | Real-time applications, interactive UI, low-latency needs. | High-throughput batch processing, massive file ingestion. |
| **Connection Handling**| Connection held open until extraction completes. | Immediately returns an `invocationArn` tracking ID. |
| **Scale & Payload** | Ideal for smaller, less complex documents (e.g., single receipts). | Ideal for large binders (up to 3,000 pages) and long videos. |
| **Timeout Risk** | Higher risk for complex/large files. | Mitigated; processing runs efficiently in the background. |

### How Async Batch Processing Works
1. **Configure:** Define extraction targets using Blueprints grouped into a Project.
2. **Invoke:** Submit the data payload with the Project ARN to the Async API.
3. **Track:** Receive an `invocationArn`. The BDA engine handles all backend orchestration (chunking, model routing).
4. **Retrieve:** Poll or use EventBridge triggers to fetch the structured JSON output upon completion.

**Example: Async Request Payload (Python Boto3)**
```python
response = runtime_client.invoke_data_automation_async(
    inputConfiguration={
        's3Uri': 's3://your-bucket/input-data/massive_binder.pdf'
    },
    outputConfiguration={
        's3Uri': 's3://your-bucket/bda-outputs/'
    },
    dataAutomationConfiguration={
        'projectArn': 'arn:aws:bedrock:us-west-2:123456789012:data-automation-project/abc123xyz'
    }
)
print(f"Tracking Job ARN: {response['invocationArn']}")
```

*Sources: [AWS Docs - BDA API](https://docs.aws.amazon.com/bedrock/latest/userguide/bda-using-api.html), [AWS Docs - InvokeDataAutomationAsync](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_data-automation-runtime_InvokeDataAutomationAsync.html)*

---

## 4. Blueprint & Project Configuration Deep Dive

You can configure custom outputs in BDA using **Blueprints**. Blueprints consist of instructions and schemas that define the list of field names to extract, the desired data format (e.g., string, number, boolean), and natural language context for validation rules. 

To process files at scale, you group your standard and custom output configurations into **Data Projects**. Within a single BDA Project, you can apply:
*   Multiple document blueprints, up to **40**. This allows you to route and process different types of documents (e.g., W2s, paystubs, invoices) within the same project.
*   **One** image blueprint.
*   **One** audio blueprint.
*   **One** video blueprint.

Projects natively support **splitting documents**, utilizing a **fallback blueprint** if a document doesn't match primary rules, and **disabling modalities** or routing file types explicitly.

**Example: Blueprint Creation via Boto3 (Python)**
```python
import boto3

client = boto3.client('bedrock-data-automation')
response = client.create_blueprint(
    blueprintName='InvoiceExtractionV1',
    type='DOCUMENT', # Can be 'DOCUMENT' or 'IMAGE'
    blueprintStage='DEVELOPMENT', # Switch to 'LIVE' for production
    schema='{"fields": [{"name": "invoice_total", "type": "number", "description": "The total amount due"}]}'
)
```

---

## 5. Security & Compliance: PII Information Redaction

For enterprise compliance, BDA tightly integrates with **Amazon Bedrock Guardrails** to seamlessly detect and redact Personally Identifiable Information (PII) before it reaches downstream systems.

*   **Ingestion:** Unstructured data (emails, PDFs, images) is ingested.
*   **Detection:** Extracted text and visual data pass through Bedrock Guardrails, which scan for sensitive entities (e.g., SSN, credit cards, names) or custom regex.
*   **Redaction Modes:**
    *   **BLOCK:** Blocks requests that contain sensitive data, returning a custom configured message.
    *   **MASK:** Redacts sensitive information by replacing it with identifier tags (e.g., `{NAME}`, `[EMAIL-1]`), ensuring content is safely anonymized.
*   **Output & Audit:** Sanitized data is delivered in the final JSON schema. The system maintains an encrypted, immutable audit trail.

**Example: Custom Regex Configuration for PII**
You can easily define a custom regex pattern to block proprietary identifiers. For example, to block a Booking ID formatted as 3 digits and 3 uppercase letters (e.g., ID123ABC):
```text
"^ID\d{3}[A-Z]{3}$"
```

*Sources: [AWS ML Blog - PII Redaction with BDA & Guardrails](https://aws.amazon.com/blogs/machine-learning/detect-and-redact-personally-identifiable-information-using-amazon-bedrock-data-automation-and-guardrails/)*

---

## 6. Multimodal Capabilities & Input Formats

BDA acts as a unified multimodal engine, routing specific file types to optimized processing pipelines. It outputs both **Standard JSON** (raw transcription/summary) and **Custom JSON** (based on natural language queries defined in your Blueprints).

| Modality | Supported Formats | Key Capabilities & Features | Scale / Limits |
| :--- | :--- | :--- | :--- |
| **Document** | PDF, DOC, DOCX | Extracts text, handwriting, complex table structures, and key-value pairs. Detects embedded hyperlinks natively. | Up to **3,000 pages** per file. Includes an intelligent "Document Splitter" to divide large binders into logical sub-documents. |
| **Image** | JPEG, PNG | High-speed OCR, visual entity extraction, and **Visual Grounding** (maps extracted insights to specific bounding box coordinates with confidence scores). | Files can be explicitly routed as "Images" or "Documents" depending on the extraction strategy. |
| **Video** | MP4, MOV, AVI, MKV, WEBM, H.265 | Analyzes visual frames and audio tracks. H.265 support enables processing of high-quality video archives at reduced file sizes and faster speeds. | MP4/MOV can be dynamically routed to either video or audio pipelines based on business need. |
| **Audio** | M4A, MP3, MP4 (routed) | Transcribes speech, extracts structured insights, key themes, and multi-speaker summaries. | Optimized for call center logs, meetings, and voice notes. |

*Sources: [AWS What's New - DOC/H.265 Support](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-bedrock-data-automation/), [AWS What's New - Modality Controls](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-bedrock-data-automation-modality-controls-hyperlinks-larger-documents/)*
