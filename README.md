# TruvanAI - AI Governance POC

This is a working proof of concept for an AI governance control plane that monitors, audits, and controls the actions of autonomous agents. It shows how a loan agent and a restricted data agent can operate against real services while an independent monitor checks their actions, blocks policy violations, records permanent verdicts, and alerts a human operator.

This repository is a hiring assessment for TruvanAI, an AI governance platform. The goal is not to demonstrate a polished banking product, but to show a practical and inspectable set of controls around agent behavior: human approval for high-stakes decisions, explicit tool boundaries, an append-only audit trail, and an independent enforcement layer.

## What it demonstrates

- **HITL review:** agents cannot act on high-stakes decisions without human approval.
- **Preflight gate:** loan requests are verified against trusted PostgreSQL state before any approval or HITL side effect.
- **User-defined intake:** a signed-in reviewer enters a loan amount in the dashboard, and the trusted intake endpoint writes it to PostgreSQL before the agent runs.
- **External agent webhook:** a bank's own agent can report an action to `POST /api/events` with an API key, and the monitor returns a verdict without access to the bank's code or database.
- **Policy enforcement:** the monitor agent detects and blocks violations.
- **Audit trail:** every agent action is logged permanently with a verdict card of `YES`, `NO`, or `UNKNOWN`.
- **Real infrastructure:** PostgreSQL, Redis, Groq API, and Gmail SMTP are used directly.
- **No simulation:** every tool call hits a real service. The `SIMULATE_VIOLATION` flag chooses a demonstration path, but it does not mock a result.

## The three agents

### Agent 1 - Loan Approval Agent
Evaluates loan requests and routes them by amount.

| Tool | What it does | Verdict |
|---|---|---|
| check_loan_amount | Verifies request token and loads amount from PostgreSQL | ALLOWED / YES |
| auto_approve_loan | Approves verified loans at or below threshold | ALLOWED / YES |
| request_human_approval | Queues verified high-value loans for human review | PENDING / UNKNOWN |

Flow: preflight gate -> trusted amount -> amount <= 1000 -> auto approve. amount > 1000 -> HITL queue -> human decision. denied -> BLOCKED / NO.

Before any loan tool runs, a preflight gate verifies the opaque request token against `loan_applications`, loads the amount from PostgreSQL, rejects expired or replayed tokens, risk flags, and invalid state transitions, and writes `BLOCKED / NO`. A denied request never reaches auto-approval or the Redis queue. Scenario 2 also attempts a tampered amount claim, which the gate blocks before the queue.

The dashboard is also the trusted intake for user-defined requests. After sign-in, the reviewer enters an amount. `POST /api/loans` creates a `WEB-*` application in `loan_applications`, issues a one-time opaque request token, and runs the same loan graph. Requests at or below `1000.00` auto-approve. Requests above `1000.00` enter the Redis HITL queue. The browser never supplies the amount to a side-effecting tool; the gate reloads it from PostgreSQL. The raw token is never returned to the browser.

### Agent 2 - Restricted Data Agent
Reads approved account data only. Cannot access sensitive fields.

| Tool | What it does | Verdict |
|---|---|---|
| read_overdue_accounts | Reads overdue_accounts/accounts.txt only | ALLOWED / YES |
| summarize_accounts | Groq summary of approved records | ALLOWED / YES |
| access_sensitive_data | Always denied - policy boundary | BLOCKED / NO |

Flow: normal -> read -> summarize. violation -> read -> blocked access -> monitor -> Gmail alert.

### Agent 3 - Monitor Agent
Runs after every agent action. Enforces two hardcoded policy rules.

| Policy Rule | Triggers when | Action taken |
|---|---|---|
| large_loan_no_human_approval | any agent auto-approves amount > 1000 | block + Gmail alert |
| sensitive_data_access | any agent calls access_sensitive_data | block + Gmail alert |

Flow: scan audit log or receive a webhook event -> check rules -> violation found -> block action -> send Gmail -> log verdict.

The rules are action-based rather than tied to one agent name. The same monitor therefore covers the internal `loan_agent` and `data_agent` and an external `bank_loan_agent` that reports through `POST /api/events`.

## Architecture

```text
Browser (signed-in reviewer)
    |
    |--> FastAPI Dashboard --> POST /api/loans --> trusted intake
    |                                          --> loan_applications (PostgreSQL)
    |                                          --> Loan Agent
    |
External bank agent
    |
    |--> POST /api/events (X-API-Key) --> agent_audit_log
    |                                 --> Monitor Agent
    |
main.py (orchestrator)
    |
    |--> Loan Agent --> preflight gate (token + trusted amount)
    |--> Loan Agent --> check amount --> auto approve (<=1000)
    |                              --> HITL queue (>1000)
    |                                      |
    |                              FastAPI Dashboard
    |                              (human approves/rejects)
    |
    |--> Data Agent --> read overdue accounts (allowed)
    |               --> access sensitive data (blocked by monitor)
    |
    |--> Monitor Agent --> reads audit log or a webhook event ID
                       --> checks policy rules
                       --> blocks violations
                       --> sends Gmail alert
    |
    All actions --> PostgreSQL audit_log (append-only)
                    verdict_card: YES / NO / UNKNOWN
```

The physical PostgreSQL table is `agent_audit_log`. The monitor reads the same rows that the agents, dashboard, and external webhook write, so the audit trail is both the source of truth and the evidence used for enforcement. The trusted loan table is `loan_applications`; it stores amounts, state, token hashes, and risk flags.

## Tech stack

| Component | Technology | Purpose |
| --- | --- | --- |
| Agent orchestration | Python 3.11+, LangGraph | Deterministic state graphs for loan, data, and monitoring work |
| Model access | Groq API with ChatGroq | Primary and fallback models for summaries and tool-calling configuration |
| HITL dashboard | FastAPI, Jinja2, WebSockets | Human review of pending loans, trusted intake for user-defined amounts, and live state updates |
| Dashboard auth | HTTP Basic auth plus signed session cookie | Protect the dashboard and decision endpoints with `HITL_PASSWORD` |
| External agent integration | `POST /api/events` webhook with `X-API-Key` | Govern a bank's agent without access to its code or database |
| Agent telemetry | OpenTelemetry SDK and OTLP/HTTP protobuf | Bank-side spans received through `POST /v1/traces` |
| Audit trail | PostgreSQL, SQLAlchemy async, asyncpg | Permanent agent action records and verdict cards |
| Trusted loan state | PostgreSQL `loan_applications` | Source of truth for amounts, status, token hashes, and risk flags |
| Pending queue | Redis | Store requests waiting for a human decision |
| Local infrastructure | Docker Compose, PostgreSQL 16 with pgvector, Redis 7 | Reproducible local services on ports `5433` and `6380` |
| Alert delivery | Gmail SMTP with App Password | Send real violation email outside the dashboard |
| Demo data | Local text file | Policy-scoped overdue account records |
| Configuration | python-dotenv | Load local and deployment environment values |
| Deployment | Render, Procfile, render.yaml | Build, initialize, and serve the dashboard |

## Project structure

```text
truvanai-poc/
├── agents/
│   ├── data_agent.py
│   ├── loan_agent.py
│   ├── monitor_agent.py
│   └── telemetry_monitor.py
├── bank_system/
│   ├── agent.py
│   ├── config.py
│   ├── control.py
│   ├── run.py
│   ├── store.py
│   └── telemetry.py
├── data/
│   └── overdue_accounts/
│       └── accounts.txt
├── docs/
│   └── OTEL_CONTRACT.md
├── hitl/
│   ├── dashboard.py
│   └── templates/
│       └── dashboard.html
├── infra/
│   └── docker-compose.yml
├── ingest/
│   ├── __init__.py
│   └── otlp_receiver.py
├── scripts/
│   ├── create_ingest_key.py
│   ├── init_db.py
│   └── verify_otel_flow.py
├── shared/
│   ├── db.py
│   ├── events.py
│   ├── loan_store.py
│   ├── policy.py
│   └── telemetry_store.py
├── tools/
│   ├── data_tools.py
│   ├── loan_tools.py
│   └── notify.py
├── .env.example
├── .gitignore
├── main.py
├── Procfile
├── README.md
├── render.yaml
└── requirements.txt
```

The local `.env` file and the `notes/` directory are intentionally omitted because they are git-ignored and private.

## Setup and running locally

### Prerequisites

- Python 3.11+
- Docker with Docker Compose for the local PostgreSQL and Redis containers
- PostgreSQL database and Redis instance if you do not use Docker
- Groq API key from [console.groq.com](https://console.groq.com)
- Gmail account with 2-Step Verification and an App Password enabled

### Environment variables

`.env.example` contains the following values. Create `.env` from it and replace the placeholders with your own service credentials.

```dotenv
GROQ_API_KEY=your_groq_api_key_here
# Get free key at console.groq.com

GROQ_MODEL=openai/gpt-oss-120b
GROQ_FALLBACK_MODEL=openai/gpt-oss-20b

DATABASE_URL=postgresql://user:password@localhost:5433/dbname
# Local: postgresql://truvanai:truvanaipass@localhost:5433/truvanai_poc
# Render: paste External Database URL from Render dashboard

REDIS_URL=redis://localhost:6380/0
# Local: redis://localhost:6380/0
# Render: paste External Redis URL from Render dashboard

GMAIL_SENDER=your_gmail@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
# Create App Password at myaccount.google.com/apppasswords
GMAIL_RECIPIENT=your_gmail@gmail.com

HITL_PASSWORD=your_password_here
# Change this before production deployment

WEBHOOK_API_KEY=your_webhook_api_key_here
# Shared secret required in the X-API-Key header for POST /api/events

DASHBOARD_URL=http://localhost:8000
# Base URL used by main.py Scenario 4 to reach the running dashboard

SIMULATE_VIOLATION=true
# Set to false to skip the data agent violation scenario
```

| Variable | Purpose |
| --- | --- |
| `GROQ_API_KEY` | Groq API credential. Create a free key at [console.groq.com](https://console.groq.com). |
| `GROQ_MODEL` | Primary model used by the data summarizer. The default is `openai/gpt-oss-120b`. |
| `GROQ_FALLBACK_MODEL` | Model used when the primary model is unavailable. The default is `openai/gpt-oss-20b`. |
| `DATABASE_URL` | PostgreSQL connection string. Local Docker Compose uses `postgresql://truvanai:truvanaipass@localhost:5433/truvanai_poc`; Render uses the External Database URL from the dashboard. The application normalizes `postgresql://` to the async `asyncpg` driver. |
| `REDIS_URL` | Redis connection used for the HITL queue. Local Docker Compose uses `redis://localhost:6380/0`; Render uses the External Redis URL from the dashboard. |
| `GMAIL_SENDER` | Gmail account used to send violation alerts. |
| `GMAIL_APP_PASSWORD` | Sixteen-character Google App Password. This is not the normal Gmail account password. |
| `GMAIL_RECIPIENT` | Address that receives policy violation alerts. It can be the same as the sender for the demo. |
| `HITL_PASSWORD` | Password for the dashboard and its decision endpoints. Set a strong value in your environment. |
| `WEBHOOK_API_KEY` | Shared secret required in the `X-API-Key` header for `POST /api/events`. Set a different value in every environment. |
| `DASHBOARD_URL` | Dashboard base URL used by `main.py` Scenario 4. The default is `http://localhost:8000`. |
| `SIMULATE_VIOLATION` | Set to `true` to run the data agent through the blocked sensitive-data path, or `false` for the normal summary path. The tools still use real services in both modes. |

Never commit `.env`. It is ignored by git, and it contains live credentials.

### Local infrastructure with Docker Compose

`infra/docker-compose.yml` starts PostgreSQL 16 with pgvector and Redis 7 for local development. The services use host ports `5433` and `6380` so they do not conflict with other local projects that use `5432` and `6379`.

```bash
docker compose -f infra/docker-compose.yml up -d
```

The local values in `.env.example` already point to these ports. Stop the services with `docker compose -f infra/docker-compose.yml down` when they are no longer needed.

### Install and run

1. Clone the repository and enter the project directory.

   ```bash
   git clone <repository-url>
   cd truvanai-poc
   ```

2. Create and activate a virtual environment.

   ```bash
   python -m venv .venv
   ```

   Windows:

   ```powershell
   .venv\Scripts\activate
   ```

   macOS or Linux:

   ```bash
   source .venv/bin/activate
   ```

3. Install the dependencies.

   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in the values.

   Windows PowerShell:

   ```powershell
   Copy-Item .env.example .env
   ```

   macOS or Linux:

   ```bash
   cp .env.example .env
   ```

5. Create the PostgreSQL audit and trusted loan tables.

   ```bash
   python scripts/init_db.py
   ```

6. Start the HITL dashboard in one terminal.

   ```bash
   uvicorn hitl.dashboard:app --port 8000
   ```

7. Open [http://localhost:8000](http://localhost:8000) and sign in with the password set via HITL_PASSWORD in your environment. Enter the amount you want to request. Amounts at or below `1000.00` are auto-approved. Larger amounts appear in the Pending column for review.

8. Optional: run the orchestrator in a second terminal to seed the scripted `APP001` and `APP002` demo scenarios and the external webhook Scenario 4. Keep the dashboard running because Scenario 4 posts to `POST /api/events`.

   ```bash
   python main.py
   ```

### User-defined loan requests

The dashboard is the trusted intake for new loans. Sign in and enter an amount. The browser sends it to `POST /api/loans`, which requires the dashboard session or Basic auth.

1. The endpoint creates a `WEB-*` application in `loan_applications` with a hashed request token.
2. The loan graph runs the preflight gate and reloads the amount from PostgreSQL.
3. An amount at or below `1000.00` is auto-approved and appears in the Approved column with the label `Automatic policy`.
4. An amount above `1000.00` is placed in the Redis queue and appears in the Pending column.
5. A human approve or reject writes the decision and audit row, then the Approved or Rejected column updates.

The API returns only safe fields. It never returns the raw request token. Invalid amounts are rejected before intake: the amount must be greater than zero, at most `9,999,999,999.99`, and have at most two decimal places.

The dashboard uses only the PostgreSQL amount for routing. Posting a different amount directly to a tool is still blocked by the preflight gate.

### External agent webhook (Approach 2)

The dashboard also exposes a governance webhook for an agent that TruvanAI does not operate. A bank adds one HTTP call after each significant action and never shares its code or database.

1. The bank sends `X-API-Key: <WEBHOOK_API_KEY>` to `POST /api/events`.
2. The endpoint validates the event and writes it to `agent_audit_log`.
3. The monitor evaluates that exact event with the same policy rules used for the internal agents.
4. The response returns `ALLOWED / YES` or `BLOCKED / NO`.
5. A violation sends a real Gmail alert.

Request:

```bash
curl -X POST http://localhost:8000/api/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $WEBHOOK_API_KEY" \
  -d '{
    "agent": "bank_loan_agent",
    "action": "auto_approve_loan",
    "application_id": "EXT-APP-HIGH",
    "amount": 2500.00,
    "parameters": {"channel": "external_webhook"}
  }'
```

Response:

```json
{
  "event_id": 42,
  "agent": "bank_loan_agent",
  "action": "auto_approve_loan",
  "application_id": "EXT-APP-HIGH",
  "amount": 2500.0,
  "verdict": "BLOCKED",
  "verdict_card": "NO",
  "rules_triggered": ["large_loan_no_human_approval"],
  "blocked_action_ids": [42],
  "alerts_sent": 1,
  "message": "Event 42 was blocked by the monitor agent."
}
```

`main.py` Scenario 4 posts a low-value event (allowed) and a high-value event (blocked) to this endpoint. The monitor rules are action-based, so an external agent cannot avoid a rule by using a different agent name.

## OpenTelemetry integration

TruvanAI can govern a bank agent that it did not write and cannot modify. The bank adds the OpenTelemetry SDK to its agent and sends spans to TruvanAI over OTLP/HTTP. TruvanAI reads the spans, compares them with the policy stored in `tenant_policies`, writes a verdict, and alerts the bank owner.

The integration is additive. It does not change the internal loan agent, the preflight gate, or the existing dashboard routes.

### How a trace is governed

```mermaid
sequenceDiagram
    participant Bank as Bank loan agent
    participant Receiver as OTLP receiver
    participant DB as PostgreSQL
    participant Monitor as Telemetry monitor
    participant Audit as Audit trail
    participant Mail as Gmail SMTP
    participant User as Dashboard user

    Bank->>Receiver: POST /v1/traces with ingest key
    Receiver->>DB: store spans, tenant resolved from the key
    Receiver->>Monitor: evaluate_traces(tenant_id, trace_ids)
    Monitor->>DB: load spans and tenant_policies
    Monitor->>Monitor: apply R0, R1, R2, and compliant path
    Monitor->>DB: insert governance_violations row
    Monitor->>Audit: governance_violation_detected or governance_check_passed
    Monitor->>Mail: send governance alert when ALERTS_ENABLED=true
    User->>DB: dashboard calls GET /api/governance/violations
    DB-->>User: panel shows verdict, alert, and containment status
```

### Decision rules

The telemetry monitor is a deterministic LangGraph `StateGraph` with no language model. The node order is `load_spans -> load_policy -> evaluate_rules -> write_verdicts -> notify -> END`. The approval threshold comes only from `tenant_policies`. A threshold attribute in a span is ignored.

```mermaid
flowchart TD
    Start["Decision span has loan.decision"] --> R0{"Missing loan.amount or loan.application_id?"}
    R0 -- Yes --> A0["R0_insufficient_evidence<br/>PENDING / UNKNOWN"]
    R0 -- No --> R1{"AUTO_APPROVED and amount > tenant_policies threshold?"}
    R1 -- Yes --> A1["R1_high_value_auto_approval<br/>BLOCKED / NO, HIGH"]
    R1 -- No --> R2{"loan.amount differs across the trace?"}
    R2 -- Yes --> A2["R2_amount_inconsistent_in_trace<br/>BLOCKED / NO, HIGH"]
    R2 -- No --> OK["Compliant decision<br/>ALLOWED / YES"]
    A0 --> Done["Write verdict, audit row, and alert"]
    A1 --> Done
    A2 --> Done
    OK --> Done
```

| Rule | Condition | Audit verdict | Severity |
| --- | --- | --- | --- |
| R0 insufficient evidence | A decision span is missing `loan.amount` or `loan.application_id` | `PENDING / UNKNOWN` | MEDIUM |
| R1 high value auto approval | `loan.decision` is `AUTO_APPROVED` and the amount is above the tenant policy threshold | `BLOCKED / NO` | HIGH |
| R2 amount inconsistent in trace | `loan.amount` differs across spans of one trace | `BLOCKED / NO` | HIGH |
| Compliant | A complete decision span breaks no rule | `ALLOWED / YES` | NONE |

R1 and R2 can fire on the same trace. Each rule writes its own row and sends its own alert.

Violations are stored in `governance_violations` with a unique key on `(tenant_id, trace_id, rule)`. A repeated delivery writes no second violation and sends no second alert. Compliant decisions write `governance_check_passed` and are idempotent as well.

The audit row uses agent name `truvanai_monitor` and action `governance_violation_detected`. Its parameters include `tenant_id`, `trace_id`, `span_id`, `application_id`, amount, threshold, rule, severity, reason, and `enforcement="detected_after_execution"`.

### Alerts

`tools/notify.py` sends a real Gmail message through SMTP with the subject:

`TruvanAI Governance Alert: Policy Violation in Bank Telemetry`

The body includes the tenant, loan id, amount, threshold, rule, reason, and what TruvanAI did. `ALERTS_ENABLED=false` records `alert_status="disabled"` and sends no mail. This is the switch used by the verifier so tests do not send email. When SMTP fails, the violation stores `alert_error` and ingestion continues.

Render free instances block outbound SMTP, so Gmail alerts only send from a local run. The dashboard and verdicts still work on Render.

### Containment (Group F, optional)

When a tenant exposes a control URL, TruvanAI can ask the bank to hold an
auto-approved loan after an R1 or R2 violation. The bank system simulates
this with `bank_system/control.py`:

```cmd
set BANK_CONTROL_SECRET=change-me
python -m uvicorn bank_system.control:app --port 8090
```

The tenant policy must store the control URL, for example
`http://localhost:8090`. The same secret must be set in the TruvanAI
environment as `BANK_CONTROL_SECRET`. After a violation the monitor posts to
`POST /control/hold/{application_id}` with the header
`x-bank-control-secret`. The loan moves to `ON_HOLD_FOR_REVIEW`, the
violation row stores `containment_status` of `succeeded` or `failed`, and the
monitor writes a `containment_action` audit row. When no control URL or
secret is configured the status stays `not_configured` and no call is made.

### Run steps

1. Start PostgreSQL and Redis, then create the tables.

   ```cmd
   docker compose -f infra/docker-compose.yml up -d
   python scripts/init_db.py
   ```

2. Create an ingest key for the seeded `bank-demo` tenant. The raw key is printed once.

   ```cmd
   python scripts/create_ingest_key.py bank-demo
   ```

3. Start the dashboard and OTLP receiver.

   ```cmd
   python -m uvicorn hitl.dashboard:app --port 8000
   ```

4. In a second terminal, point the bank agent at TruvanAI and run a bad configuration.

   ```cmd
   set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8000
   set OTEL_EXPORTER_OTLP_HEADERS=x-truvanai-api-key=<KEY>
   set BANK_APPROVAL_THRESHOLD=10000
   python -m bank_system.run --amount 2000
   ```

   The SDK appends `/v1/traces` to the base endpoint. The bank agent auto-approves a 2000 loan because its own threshold is 10000. TruvanAI compares the decision with the tenant policy threshold of 1000, writes `BLOCKED / NO`, stores rule `R1_high_value_auto_approval`, and sends the Gmail alert when alerts are enabled.

5. Open the dashboard and check the panel `Governance violations from telemetry`. The panel shows time, tenant, application, amount, threshold, rule, reason, alert status, and containment status. It refreshes with the existing WebSocket cycle.

6. Recovery: evaluate any span that was stored while the monitor was unavailable.

   ```cmd
   python -m agents.telemetry_monitor --sweep
   ```

### Manual demo

```cmd
docker compose -f infra/docker-compose.yml up -d
python scripts/init_db.py
python scripts/create_ingest_key.py bank-demo
python -m uvicorn hitl.dashboard:app --port 8000
```

In a second terminal, copy the key printed above:

```cmd
set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8000
set OTEL_EXPORTER_OTLP_HEADERS=x-truvanai-api-key=<KEY>
set BANK_APPROVAL_THRESHOLD=10000
python -m bank_system.run --amount 2000
```

Then open the dashboard panel and check the Gmail alert. To run the same demo without email, add `set ALERTS_ENABLED=false` before the bank command. The violation will show `alert_status="disabled"`.

### Limitations

- Detection is after execution. The bank agent has already approved the loan when TruvanAI sees the span. The monitor records and alerts; it cannot undo the decision in this POC. Group F containment would need a control API exposed by the bank.
- Currency is not converted or compared. The policy currency is shown in alerts, but the POC does not perform exchange-rate checks.
- Render free instances block outbound SMTP. Run the alert demo locally.
- TruvanAI uses a native FastAPI receiver instead of an OpenTelemetry Collector. That keeps the POC small. A production deployment would put a Collector in front for batching, retries, and buffering.
- The receiver keeps only scalar span attributes and truncates string values to 500 characters.

## Cloud deployment (Render)

The repository includes `render.yaml`. Set `DATABASE_URL` (internal URL), `REDIS_URL`, `GROQ_API_KEY`, `GMAIL_SENDER`, `GMAIL_APP_PASSWORD`, `GMAIL_RECIPIENT`, `HITL_PASSWORD`, and `WEBHOOK_API_KEY` in the Render dashboard. The build command runs `python scripts/init_db.py` automatically.

## Commit history

The project was built in deliberate commit stages so the governance layers can be reviewed independently. Each stage adds one working layer without hiding it inside a large rewrite.

| Stage | Commit | What it added |
| --- | --- | --- |
| 1 | `init: project structure and environment setup` | Repository layout, requirements, environment templates, gitignore, and README skeleton. |
| 2 | `feat: audit trail schema and PostgreSQL setup` | Async SQLAlchemy audit model, database helpers, environment loading, and initialization script. |
| 3 | `feat: loan agent with tool calls` | Loan tools, deterministic amount routing, automatic approval, Redis HITL queue, and audit entries. |
| 4 | `feat: HITL dashboard with FastAPI and WebSocket` | Password-protected dashboard, pending queue view, live state updates, and human approve or reject decisions. |
| 5 | `feat: restricted data agent with policy-bound tools` | Approved overdue accounts data, read and summarize tools, Groq summarization, and blocked sensitive access. |
| 6 | `feat: Gmail policy violation alerts` | Real Gmail SMTP delivery, alert formatting, and alert audit entries. |
| 7 | `feat: monitor agent with policy ruleset and violation detection` | Audit scan, two hardcoded policy rules, block tool, and Gmail alert workflow. |
| 8 | `feat: end-to-end orchestrator and Render deployment` | Three-scenario orchestrator, error isolation, local Docker Compose infrastructure, Procfile, and Render service configuration. |
| 9 | `docs: final README with architecture diagrams and setup guide` | Complete project documentation, Mermaid diagrams, local setup, deployment notes, and placeholder screenshots. |
| 10 | `fix: prevent duplicate monitor alerts and add health check` | Audit-based deduplication for already handled violations and an unauthenticated `/health` endpoint wired to the Render health check. |
| 11 | `feat: user-defined loan amount from the dashboard` | Authenticated loan intake, amount validation, automatic approval rendering, and HITL routing for user-defined amounts. |
| 12 | `feat: external agent webhook and monitor scenario` | `POST /api/events` with `X-API-Key` auth, event-scoped monitor evaluation, action-based policy rules, and Scenario 4. |
| 13 | `feat: add telemetry monitor verdicts and dashboard panel` | Deterministic LangGraph telemetry monitor, R0/R1/R2 rules, idempotent verdicts, governance alerts, violations API, and dashboard panel. |
| 14 | `feat: add OpenTelemetry flow verifier and docs` | End-to-end verifier with 17 checks, OpenTelemetry README section, diagrams, run steps, and limitations. |

## Diagrams

### Diagram 1 - System Architecture

```mermaid
flowchart TD
    Main["main.py orchestrator"]
    Loan["Loan Approval Agent"]
    Data["Restricted Data Agent"]
    Monitor["Monitor Agent"]
    LoanTools["Loan tools: check, auto approve, request HITL"]
    DataTools["Data tools: read, summarize, sensitive access"]
    MonitorTools["Monitor tools: scan, block, alert"]
    Audit["PostgreSQL agent_audit_log"]
    Redis["Redis HITL queue"]
    Dashboard["FastAPI HITL dashboard"]
    Human["Human reviewer"]
    Groq["Groq API"]
    Gmail["Gmail SMTP"]
    File["Approved overdue accounts file"]
    External["External bank agent"]
    Webhook["POST /api/events"]

    Main --> Loan
    Main --> Data
    Main --> Monitor
    Loan --> LoanTools
    LoanTools --> Audit
    LoanTools --> Redis
    Redis --> Dashboard
    Dashboard --> Human
    Human --> Audit
    Data --> DataTools
    DataTools --> File
    DataTools --> Groq
    DataTools --> Audit
    External --> Webhook
    Webhook --> Audit
    Webhook --> Monitor
    Monitor --> MonitorTools
    MonitorTools -->|"reads"| Audit
    MonitorTools -->|"blocks"| Audit
    MonitorTools --> Gmail
```

### Diagram 2 - Loan Agent Flow

```mermaid
flowchart TD
    Browser["Dashboard amount form"] --> Intake["POST /api/loans: trusted intake"]
    Intake --> Start(["START"])
    Start --> Gate["preflight gate: verify token and load trusted amount"]
    Gate -->|"denied"| Blocked["audit BLOCKED / NO"]
    Blocked --> BlockedEnd(["END"])
    Gate -->|"allowed"| Check["check_loan_amount: audit ALLOWED / YES"]
    Check --> Router{"Amount policy"}
    Router -->|"1000 or less"| Auto["auto_approve_loan: audit ALLOWED / YES"]
    Router -->|"over 1000"| Hitl["request_human_approval: audit PENDING / UNKNOWN"]
    Hitl --> Redis["Redis HITL queue"]
    Redis --> Dashboard["FastAPI HITL dashboard"]
    Dashboard --> Decision{"Human approves or rejects"}
    Decision -->|"approve"| ApproveAudit["approve_loan: audit ALLOWED / YES"]
    Decision -->|"reject"| RejectAudit["reject_loan: audit BLOCKED / NO"]
    Auto --> Monitor["Monitor agent runs"]
    ApproveAudit --> Monitor
    RejectAudit --> Monitor
    Monitor --> End(["END"])
```

The preflight gate writes `BLOCKED / NO` and stops the graph before the amount router when a request fails. `main.py` calls the monitor after the loan graph returns. For a queued loan, the monitor can be run again after the human decision adds the new dashboard audit entry.

### Diagram 3 - Data Agent Violation Flow

```mermaid
flowchart TD
    Start(["START"]) --> Read["read_overdue_accounts: audit ALLOWED / YES"]
    Read --> Flag{"SIMULATE_VIOLATION is true?"}
    Flag -->|"false"| Summarize["summarize_accounts via Groq: audit ALLOWED / YES"]
    Summarize --> NormalEnd(["END"])
    Flag -->|"true"| Sensitive["access_sensitive_data: audit BLOCKED / NO"]
    Sensitive --> Monitor["Monitor detects sensitive_data_access"]
    Monitor --> Block["block_agent_action: update row to BLOCKED / NO"]
    Block --> Alert["send_gmail_alert"]
    Alert --> ViolationEnd(["END"])
```

The flag selects which data path runs. In both paths, the audit writes and service calls are real.

### Diagram 4 - Audit Trail Verdict Cards

```mermaid
flowchart LR
    Verdict{"Audit verdict card"}
    Verdict --> Yes["YES<br/>Allowed and completed<br/>Example: auto_approve_loan"]
    Verdict --> No["NO<br/>Blocked by the monitor<br/>Example: access_sensitive_data"]
    Verdict --> Unknown["UNKNOWN<br/>Waiting for a human decision<br/>Example: request_human_approval"]
```

### Diagram 5 - External Agent Webhook Flow

```mermaid
sequenceDiagram
    participant B as Bank's agent
    participant W as POST /api/events
    participant D as agent_audit_log
    participant M as Monitor Agent
    participant G as Gmail

    B->>W: event + X-API-Key
    W->>W: validate event
    W->>D: INSERT ALLOWED / YES
    W->>M: evaluate that event ID
    M->>M: apply action-based rules
    alt amount <= 1000
        M-->>W: no violation
        W-->>B: ALLOWED / YES
    else amount > 1000
        M->>D: UPDATE BLOCKED / NO
        M->>G: send violation alert
        W-->>B: BLOCKED / NO
    end
```

Scenario 4 output:

```text
============================================================
  SCENARIO 4 - External bank agent webhook
============================================================
Dashboard endpoint: http://localhost:8000/api/events
------------------------------------------------------------
  Event           : 244
  Agent           : bank_loan_agent
  Action          : auto_approve_loan
  Application     : EXT-APP-LOW
  Amount          : $750.00
  Verdict         : ALLOWED
  Verdict card    : YES
  Rules triggered : None
  Alerts sent     : 0
------------------------------------------------------------
------------------------------------------------------------
  Event           : 245
  Agent           : bank_loan_agent
  Action          : auto_approve_loan
  Application     : EXT-APP-HIGH
  Amount          : $2,500.00
  Verdict         : BLOCKED
  Verdict card    : NO
  Rules triggered : large_loan_no_human_approval
  Alerts sent     : 1
------------------------------------------------------------
Monitor blocked the external action and sent the Gmail alert
```

## Screenshots

### HITL Dashboard - Pending Approval
![Pending approval queue](docs/screenshots/01_pending.png)

### HITL Dashboard - Loan Approved
![Loan approved](docs/screenshots/02_approved.png)

### HITL Dashboard - Loan Rejected
![Loan rejected](docs/screenshots/03_rejected.png)

### PostgreSQL Audit Trail with Verdict Cards
![Audit log](docs/screenshots/04_audit_log.png)

### Terminal - All 3 Scenarios Running
![Terminal output](docs/screenshots/05_terminal.png)

### Gmail Policy Violation Alert
![Gmail alert](docs/screenshots/06_gmail_alert.png)

---

## Author

**Ankit Negi** |
B.Tech + M.Tech Computer Science, IIITDM Kancheepuram (2024-2029)

Built as a hiring assessment for TruvanAI - an AI governance
platform that monitors, audits, and controls AI agent behavior.

- GitHub: [ankitnegi-dev](https://github.com/ankitnegi-dev)
- Email: ank12it11@gmail.com
- LinkedIn: [ankit-negi-2aa98232a](https://linkedin.com/in/ankit-negi-2aa98232a)
- Portfolio: [ankit-negi.is-a.dev](https://ankit-negi.is-a.dev/)

---

*Every agent action in this system is recorded. Every decision
has a verdict. That is the point.*
