# TruvanAI - AI Governance POC

This is a working proof of concept for an AI governance control plane that monitors, audits, and controls the actions of autonomous agents. It shows how a loan agent and a restricted data agent can operate against real services while an independent monitor checks their actions, blocks policy violations, records permanent verdicts, and alerts a human operator.

This repository is a hiring assessment for TruvanAI, an AI governance platform. The goal is not to demonstrate a polished banking product, but to show a practical and inspectable set of controls around agent behavior: human approval for high-stakes decisions, explicit tool boundaries, an append-only audit trail, and an independent enforcement layer.

## What it demonstrates

- **HITL review:** agents cannot act on high-stakes decisions without human approval.
- **Policy enforcement:** the monitor agent detects and blocks violations.
- **Audit trail:** every agent action is logged permanently with a verdict card of `YES`, `NO`, or `UNKNOWN`.
- **Real infrastructure:** PostgreSQL, Redis, Groq API, and Gmail SMTP are used directly.
- **No simulation:** every tool call hits a real service. The `SIMULATE_VIOLATION` flag chooses a demonstration path, but it does not mock a result.

## The three agents

### Agent 1 - Loan Approval Agent

The Loan Approval Agent evaluates a loan request, checks the amount against the policy threshold, and either approves a low-value loan automatically or queues a high-value loan for a human decision. It uses a deterministic LangGraph flow so the same amount always reaches the same policy branch. Groq is configured against `GROQ_MODEL` with the fallback model for tool-calling use cases, while the approval decision remains rule-based.

**Tools it has**

- `check_loan_amount(application_id, amount)`: validates the request, compares the amount with the `$1,000` threshold, and writes an `ALLOWED / YES` audit entry.
- `auto_approve_loan(application_id)`: approves an application at or below the threshold and writes an `ALLOWED / YES` entry.
- `request_human_approval(application_id, amount)`: pushes a pending request to the Redis queue and writes a `PENDING / UNKNOWN` entry.

**Normal flow**

`START` -> `loan_checker` -> amount router -> `auto_approver` -> `END` for applications at or below `$1,000`. The automatic approval is written to PostgreSQL, and the orchestrator then runs the monitor.

**HITL flow**

`START` -> `loan_checker` -> amount router -> `hitl_requester` -> Redis -> FastAPI dashboard -> human approve or reject -> audit -> `END` for applications above `$1,000`. The dashboard records `approve_loan` as `ALLOWED / YES` or `reject_loan` as `BLOCKED / NO`. The human decision is a separate audited action by `hitl_dashboard`.

### Agent 2 - Restricted Data Agent

The Restricted Data Agent reads only the approved overdue accounts file and asks Groq for a short summary. Its tools have explicit boundaries, so a sensitive field request is denied even when the agent asks for it. The graph can take the normal summary path or the policy violation path based on `SIMULATE_VIOLATION`.

**Tools it has**

- `read_overdue_accounts()`: reads only `data/overdue_accounts/accounts.txt` and writes an `ALLOWED / YES` audit entry.
- `summarize_accounts(raw_data)`: sends the approved records to the primary Groq model with the fallback model and writes an `ALLOWED / YES` entry.
- `access_sensitive_data(field)`: always returns an access denial and writes a `BLOCKED / NO` entry for the attempted field.

**Normal flow**

`START` -> `data_reader` -> `summarizer` -> Groq summary -> audit `ALLOWED` -> `END`. The orchestrator then runs the monitor. No sensitive field is read, requested, or returned.

**Violation flow**

`START` -> `data_reader` -> `violation_node` -> `access_sensitive_data` -> audit `BLOCKED / NO` -> monitor scan -> `block_agent_action` -> Gmail alert -> `END`. The tool denies the access at the boundary, and the monitor independently records the violation and alerts the operator.

### Agent 3 - Monitor Agent

The Monitor Agent reviews the audit trail after agent work and enforces the two hardcoded policy rules. It examines the 10 newest audit rows, and `run_monitor(agent_name="data_agent")` can limit the scan to one agent. Policy decisions are deterministic. ChatGroq is configured through `GROQ_MODEL`, but the model has no authority over blocking.

Before blocking, the monitor checks the audit trail for an existing `block_agent_action` entry for the same action id. Actions that were already handled are skipped, so later monitor runs do not block the same row again or send a duplicate Gmail alert.

**Policy rules it enforces**

1. `large_loan_no_human_approval`: triggers for a `loan_agent` action named `auto_approve_loan` when any recent action for the same `application_id` records an `amount` greater than `1000`.
2. `sensitive_data_access`: triggers for a `data_agent` action named `access_sensitive_data`.

**What happens on a violation**

The monitor calls `block_agent_action(agent_id, action_id)`, which changes the original audit row to `BLOCKED / NO` and appends a new `monitor_agent` audit entry with `policy_status=ALLOWED` and `verdict_card=YES`. It then calls `send_gmail_alert` with the full violation details. The final state records the violation, the blocked action id, the alert confirmation, and `monitoring_complete=True`.

## Architecture

```text
main.py (orchestrator)
    |
    |--> Loan Agent --> check amount --> auto approve (<=1000)
    |                              --> HITL queue (>1000)
    |                                      |
    |                              FastAPI Dashboard
    |                              (human approves/rejects)
    |
    |--> Data Agent --> read overdue accounts (allowed)
    |               --> access sensitive data (blocked by monitor)
    |
    |--> Monitor Agent --> reads audit log
                       --> checks policy rules
                       --> blocks violations
                       --> sends Gmail alert
    |
    All actions --> PostgreSQL audit_log (append-only)
                    verdict_card: YES / NO / UNKNOWN
```

The physical PostgreSQL table is `agent_audit_log`. The monitor reads the same rows that the agents and dashboard write, so the audit trail is both the source of truth and the evidence used for enforcement.

## Tech stack

| Component | Technology | Purpose |
| --- | --- | --- |
| Agent orchestration | Python 3.11+, LangGraph | Deterministic state graphs for loan, data, and monitoring work |
| Model access | Groq API with ChatGroq | Primary and fallback models for summaries and tool-calling configuration |
| HITL dashboard | FastAPI, Jinja2, WebSockets | Human review of pending loans with live state updates |
| Dashboard auth | HTTP Basic auth plus signed session cookie | Protect the dashboard and decision endpoints with `HITL_PASSWORD` |
| Audit trail | PostgreSQL, SQLAlchemy async, asyncpg | Permanent agent action records and verdict cards |
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
│   └── monitor_agent.py
├── data/
│   └── overdue_accounts/
│       └── accounts.txt
├── hitl/
│   ├── dashboard.py
│   └── templates/
│       └── dashboard.html
├── infra/
│   └── docker-compose.yml
├── scripts/
│   └── init_db.py
├── shared/
│   └── db.py
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

HITL_PASSWORD=truvanai2024
# Change this before production deployment

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
| `HITL_PASSWORD` | Password for the dashboard and its decision endpoints. Replace the demo value before production. |
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

5. Create the PostgreSQL audit table.

   ```bash
   python scripts/init_db.py
   ```

6. Start the HITL dashboard in one terminal.

   ```bash
   uvicorn hitl.dashboard:app --port 8000
   ```

7. Run the orchestrator in a second terminal.

   ```bash
   python main.py
   ```

8. Open [http://localhost:8000](http://localhost:8000) and sign in with `HITL_PASSWORD`. The demo password is `truvanai2024`.

Start the dashboard before the orchestrator so the large loan request can appear in the live queue. The orchestrator initializes the database, runs the small loan, large loan, and data agent scenarios, calls the monitor after each agent path, and waits two seconds between the loan scenarios.

## Cloud deployment (Render)

The repository includes `render.yaml` for a Render web service. Render installs `requirements.txt`, runs `python scripts/init_db.py` during the build, and starts the FastAPI dashboard with the provided start command. The `Procfile` contains the same web process definition for platforms that use it.

The dashboard exposes an unauthenticated `/health` endpoint that returns `{"status": "ok"}`, and `render.yaml` configures `healthCheckPath: /health` so Render can check the service without dashboard credentials.

Set the `sync: false` values in the Render dashboard: `DATABASE_URL`, `REDIS_URL`, `GROQ_API_KEY`, `GMAIL_SENDER`, `GMAIL_APP_PASSWORD`, and `GMAIL_RECIPIENT`. Paste the External Database URL and External Redis URL from Render into `DATABASE_URL` and `REDIS_URL`; the local Docker Compose ports are not used in the cloud. The YAML sets `GROQ_MODEL`, `HITL_PASSWORD`, and `SIMULATE_VIOLATION`; change the demo password before exposing the service publicly. Render provides the `PORT` value used by the start command.

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
    Monitor --> MonitorTools
    MonitorTools -->|"reads"| Audit
    MonitorTools -->|"blocks"| Audit
    MonitorTools --> Gmail
```

### Diagram 2 - Loan Agent Flow

```mermaid
flowchart TD
    Start(["START"]) --> Check["check_loan_amount: audit ALLOWED / YES"]
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

`main.py` calls the monitor after the loan graph returns. For a queued loan, the monitor can be run again after the human decision adds the new dashboard audit entry.

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
