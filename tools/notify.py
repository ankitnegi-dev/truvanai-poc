"""Gmail alert delivery for TruvanAI governance violations.

This module performs real Gmail SMTP delivery through smtplib. It does not
simulate email delivery. Missing credentials and SMTP errors are raised to the
caller so a failed alert is never reported as successful.
"""

from __future__ import annotations

import asyncio
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.tools import tool

from shared.db import log_action

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
ALERT_SUBJECT = (
    "TruvanAI - AI Governance Alert: Policy Violation Detected"
)
AGENT_NAME = "monitor_agent"


def send_email(to: str, subject: str, body: str) -> None:
    """Send a plain text email through Gmail SMTP with STARTTLS."""
    sender = os.getenv("GMAIL_SENDER", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()

    missing: list[str] = []
    if not sender:
        missing.append("GMAIL_SENDER")
    if not app_password:
        missing.append("GMAIL_APP_PASSWORD")
    if missing:
        raise RuntimeError(
            "Missing Gmail credentials: "
            + ", ".join(missing)
            + ". Add them to .env before sending alerts."
        )

    if not isinstance(to, str) or not to.strip():
        raise ValueError("Email recipient must be a non-empty string.")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("Email subject must be a non-empty string.")
    if not isinstance(body, str):
        raise TypeError("Email body must be a string.")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to.strip()
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(sender, app_password)
        smtp.send_message(message)


def _gmail_recipient() -> str:
    recipient = os.getenv("GMAIL_RECIPIENT", "").strip()
    if not recipient:
        raise RuntimeError(
            "GMAIL_RECIPIENT is not set. Add the alert recipient to .env."
        )
    return recipient


def _format_parameters(parameters: Any) -> str:
    if isinstance(parameters, (dict, list)):
        return json.dumps(
            parameters,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    return str(parameters)


@tool
async def send_gmail_alert(violation_details: dict[str, Any]) -> str:
    """Send a Gmail alert for a blocked AI policy violation."""
    if not isinstance(violation_details, dict):
        raise TypeError("violation_details must be a dictionary.")

    required_fields = ("agent_name", "action_name", "parameters", "timestamp")
    missing_fields = [
        field
        for field in required_fields
        if field not in violation_details
        or violation_details[field] is None
        or str(violation_details[field]).strip() == ""
    ]
    if missing_fields:
        raise ValueError(
            "violation_details is missing required fields: "
            + ", ".join(missing_fields)
        )

    verdict = str(violation_details.get("verdict") or "BLOCKED").strip().upper()
    if verdict != "BLOCKED":
        raise ValueError("Gmail alerts only support BLOCKED violations.")

    recipient = _gmail_recipient()
    agent_name = str(violation_details["agent_name"]).strip()
    action_name = str(violation_details["action_name"]).strip()
    parameters = _format_parameters(violation_details["parameters"])
    timestamp = str(violation_details["timestamp"]).strip()

    body = "\n".join(
        [
            "---",
            "TruvanAI AI Governance Alert",
            "",
            "A policy violation was detected and blocked.",
            "",
            f"Agent: {agent_name}",
            f"Action attempted: {action_name}",
            f"Parameters: {parameters}",
            f"Time: {timestamp}",
            "Verdict: BLOCKED",
            "",
            "This action was automatically blocked by the monitor agent.",
            "No data was accessed or modified.",
            "",
            "- TruvanAI Governance System",
            "---",
        ]
    )

    await asyncio.to_thread(
        send_email,
        recipient,
        ALERT_SUBJECT,
        body,
    )

    confirmation = f"Alert sent to {recipient}"
    await log_action(
        agent_name=AGENT_NAME,
        action_name="send_gmail_alert",
        parameters={
            "recipient": recipient,
            "violation_agent": agent_name,
            "violation_action": action_name,
        },
        result=confirmation,
        policy_status="ALLOWED",
        verdict_card="YES",
    )
    return confirmation
