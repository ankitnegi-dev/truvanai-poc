# OTel Span Contract for Bank Integrations

This document describes the span names, attributes, and resource fields that
a bank must produce for TruvanAI to evaluate governance rules from telemetry.

---

## How tenancy works

A bank is identified by its ingest key, not by any attribute in the span.

Every span ingested through POST /v1/traces is credited to the tenant whose
ingest key signed the request. Any tenant_id attribute in the payload is
ignored. A compromised key can only affect the tenant it belongs to.

To obtain an ingest key:

    python scripts/create_ingest_key.py bank-demo

The raw key is printed once and never stored. Set it as:

    OTEL_EXPORTER_OTLP_HEADERS=x-truvanai-api-key=<KEY>

---

## OTLP endpoint

The OTel SDK appends /v1/traces to OTEL_EXPORTER_OTLP_ENDPOINT.

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8000

The SDK exports to http://localhost:8000/v1/traces.
Do NOT include /v1/traces in the endpoint env var.

---

## Resource attributes

| Attribute    | Value             | Required |
|---|---|---|
| service.name | bank-loan-agent   | Yes      |

---

## Span contract

### loan.process (root span)

The root span for one loan application.

| Attribute          | Type   | Example      | Required |
|---|---|---|---|
| loan.application_id | string | LOAN-X       | Yes      |
| loan.amount         | double | 2000.0       | Yes      |
| loan.currency       | string | INR          | Yes      |
| agent.name          | string | bank_loan_agent | Yes   |

### agent.tool.check_amount (child of loan.process)

Emitted when the agent reads the loan amount and checks the routing policy.

| Attribute          | Type   | Example | Required |
|---|---|---|---|
| loan.application_id | string | LOAN-X  | Yes      |
| loan.amount         | double | 2000.0  | Yes      |

### agent.tool.auto_approve (child of loan.process)

Emitted when the bank agent auto-approves the loan.

| Attribute          | Type   | Example       | Required |
|---|---|---|---|
| loan.application_id | string | LOAN-X        | Yes      |
| loan.amount         | double | 2000.0        | Yes      |
| loan.decision       | string | AUTO_APPROVED | Yes      |

### agent.tool.request_human_review (child of loan.process)

Emitted when the bank agent routes the loan to a human reviewer.

| Attribute          | Type   | Example       | Required |
|---|---|---|---|
| loan.application_id | string | LOAN-X        | Yes      |
| loan.amount         | double | 2000.0        | Yes      |
| loan.decision       | string | SENT_TO_HITL  | Yes      |

---

## Decision rules evaluated by TruvanAI

TruvanAI reads only the tenant policy stored in tenant_policies. The bank's
own threshold is never read from spans.

| Rule | Condition | Verdict |
|---|---|---|
| high_value_auto_approval (R1) | AUTO_APPROVED and amount > policy threshold | BLOCKED / NO |
| amount_inconsistent_in_trace (R2) | loan.amount differs across spans of one trace | BLOCKED / NO |
| insufficient_evidence (R0) | Decision span missing loan.amount or loan.application_id | PENDING / UNKNOWN |
| (compliant) | AUTO_APPROVED and amount <= threshold | ALLOWED / YES |
| (compliant) | SENT_TO_HITL regardless of amount | ALLOWED / YES |

---

## Sample trace

A 2000 INR loan auto-approved by a bank with threshold 10000
(bad config, TruvanAI policy threshold is 1000):

```
ResourceSpans
  resource.attributes:
    service.name = "bank-loan-agent"
  ScopeSpans
    Span: loan.process
      trace_id: <16 bytes>
      span_id:  <8 bytes>
      attributes:
        loan.application_id = "LOAN-X"
        loan.amount         = 2000.0
        loan.currency       = "INR"
        agent.name          = "bank_loan_agent"
    Span: agent.tool.check_amount
      parent_span_id: <loan.process span_id>
      attributes:
        loan.application_id = "LOAN-X"
        loan.amount         = 2000.0
    Span: agent.tool.auto_approve
      parent_span_id: <loan.process span_id>
      attributes:
        loan.application_id = "LOAN-X"
        loan.amount         = 2000.0
        loan.decision       = "AUTO_APPROVED"
```

TruvanAI detects that AUTO_APPROVED with amount 2000 exceeds the policy
threshold of 1000, writes a BLOCKED / NO audit row, and sends a Gmail alert.

---

## Limitations (this POC)

- Detection is after execution. TruvanAI cannot stop an action the bank has
  already taken. It detects the violation from telemetry and records it.
- Currency is not converted or compared. The currency attribute is stored and
  displayed but not used in the threshold comparison. All amounts are compared
  as numeric values regardless of currency.
- SMTP alerts are blocked on Render free tier. Run locally for Gmail alerts.
- The receiver is a native FastAPI endpoint, not a standard OTel Collector.
  This simplifies the POC but means OTel Collector features (sampling,
  batching policies, fan-out) are not available.
