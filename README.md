# TruvanAI — AI Governance POC

> A working proof of concept for policy-governed AI agents: every decision is logged, high-value actions wait for a human, and policy violations are blocked and reported.

**Status:** Work in progress — built in reviewable commit stages.

## What this project demonstrates

TruvanAI explores a practical control plane for agentic systems:

- **Human-in-the-loop (HITL)** approval for consequential actions.
- **Restricted data access** with explicit tool boundaries.
- **Independent monitoring** and immediate blocking of policy violations.
- **A permanent audit trail** for every action and verdict.
- **Operational alerting** when an agent crosses a policy boundary.

## Agents

| Agent | Responsibility | Guardrail |
| --- | --- | --- |
| Loan Approval Agent | Evaluates loan applications and approves low-value requests | Loans over $1,000 require human approval |
| Restricted Data Agent | Summarizes overdue accounts from an approved file | Sensitive fields outside its allowlist are blocked |
| Monitor Agent | Reviews the audit log and enforces policy | Violations are blocked and trigger a Gmail alert |

## Planned architecture

```text
+-------------------+       +---------------------+       +------------------+
| Loan Agent        |       | Restricted Data     |       | Monitor Agent    |
|                   |       | Agent               |       |                  |
| auto-approve      |       | approved file only  |       | policy checks    |
| or HITL request   |       | allowlisted fields  |       | block + alert    |
+---------+---------+       +----------+----------+       +--------+---------+
          |                            |                           |
          +----------------------------+---------------------------+
                                       v
                           +-------------------------+
                           | PostgreSQL audit trail  |
                           | agent_audit_log         |
                           +------------+------------+
                                        |
                     +------------------+------------------+
                     |                                     |
                     v                                     v
          +---------------------+                +--------------------+
          | Redis HITL queue    |                | Gmail SMTP alerts  |
          | FastAPI + WebSocket |                +--------------------+
          +---------------------+
```

## Stack

- Python 3.12
- LangGraph and the Groq API (primary: `openai/gpt-oss-120b`, fallback: `openai/gpt-oss-20b`)
- FastAPI with WebSockets for the HITL dashboard
- PostgreSQL for the append-only audit trail
- Redis for the pending-approval queue
- Gmail SMTP with an App Password for alerts
- Render for hosting

## Planned repository layout

```text
truvanai-poc/
├── agents/                 # Loan, data, and monitor agents
├── data/overdue_accounts/  # Policy-scoped demo data
├── hitl/                   # FastAPI HITL service and dashboard
├── shared/                 # Database access and shared models
├── scripts/                # Database initialization
├── tools/                  # Agent tools and Gmail notifier
├── docs/screenshots/       # Demo screenshots
├── main.py                 # Scenario orchestrator
├── Procfile                # Render process definition
└── render.yaml             # Render infrastructure configuration
```

## Local setup

> Full setup instructions will be added as the implementation stages land.

Prerequisites:

- Python 3.12+
- PostgreSQL
- Redis
- A Groq API key
- A Gmail account with 2-Step Verification and an App Password

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the service credentials. Never commit `.env`.

## Monitoring and policy

The monitor will apply these rules after agent actions:

1. A loan over $1,000 approved without recorded human approval is a violation.
2. Access to any data field outside `overdue_amount`, `account_id`, and `name` is a violation.

Violations are blocked immediately, written to the audit trail with a `BLOCKED` verdict, and reported by email.

## Screenshots

_Placeholder — screenshots will be added after the full demo is running._

| View | Preview |
| --- | --- |
| HITL dashboard | _Coming soon_ |
| Pending approval email | _Coming soon_ |
| Blocked-action alert | _Coming soon_ |

## Deployment

The completed app will deploy to Render with a managed PostgreSQL database, Redis instance, and environment variables configured in the Render dashboard. Deployment commands will be documented in the final README.
