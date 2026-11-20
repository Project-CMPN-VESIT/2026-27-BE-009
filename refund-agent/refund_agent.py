"""Poll received return issues, execute an idempotent demo refund, and notify customers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import smtplib
import time
from dataclasses import dataclass
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()
LOGGER = logging.getLogger("refund_agent")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


SKU_HEADING_RE = re.compile(r"^##\s+([A-Z]+-\d+)\b", re.M)
ALLOWANCE_RE = re.compile(r"\*\*Refund Allowance:\*\*\s*₹\s*([\d,]+(?:\.\d+)?)")
DEDUCTION_RATE_RE = re.compile(
    r"\*\*Processing deduction rate:\*\*\s*(\d+(?:\.\d+)?)\s*%",
    re.I,
)
SKU_TOKEN_RE = re.compile(r"\b([A-Z]{2,4}-\d{3})\b", re.I)
TABLE_ROW_RE = re.compile(
    r"\|\s*([A-Z]+-\d+)\s*\|\s*([^|]+?)\s*\|\s*₹\s*([\d,]+(?:\.\d+)?)\s*\|",
    re.I,
)


@dataclass(frozen=True)
class RefundIssue:
    issue_id: UUID
    customer_id: UUID
    customer_email: str
    customer_name: str
    order_number: str
    issue_title: str
    issue_description: str


@dataclass(frozen=True)
class OrderLine:
    sku: str
    name: str
    quantity: int
    unit_price: float
    catalog_price: float
    refund_allowance: float | None


def database_url() -> str:
    value = os.getenv("SUPABASE_DB_URL", "").strip()
    if not value:
        raise RuntimeError("SUPABASE_DB_URL is required")
    return value


def get_connection():
    return psycopg2.connect(database_url(), connect_timeout=10)


def ensure_tables() -> None:
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS refund_agent_transactions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                issue_id UUID NOT NULL UNIQUE,
                order_number TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                amount_inr NUMERIC(12, 2) NOT NULL,
                payment_id TEXT NOT NULL,
                transaction_id TEXT NOT NULL,
                refund_id TEXT NOT NULL,
                gateway_status TEXT NOT NULL,
                gateway_response JSONB NOT NULL DEFAULT '{}'::jsonb,
                notified_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS refund_allowance NUMERIC(12, 2)
            """
        )
        cursor.execute(
            """
            UPDATE products
            SET refund_allowance = price
            WHERE refund_allowance IS NULL AND price IS NOT NULL
            """
        )
        cursor.execute(
            """
            ALTER TABLE customer_issues
            ADD COLUMN IF NOT EXISTS agent_decision TEXT DEFAULT ''
            """
        )
        cursor.execute(
            """
            ALTER TABLE refund_agent_transactions
            DROP CONSTRAINT IF EXISTS refund_agent_transactions_issue_id_key
            """
        )
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS refund_agent_transactions_order_number_key
            ON refund_agent_transactions (order_number)
            """
        )


def find_received_issues(limit: int = 20) -> list[RefundIssue]:
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ci.id, ci.customer_id, c.email, c.name, ci.order_number,
                   ci.issue_title, ci.issue_description
            FROM customer_issues ci
            JOIN customers c ON c.id::text = ci.customer_id::text
            LEFT JOIN refund_agent_transactions rat ON rat.order_number = ci.order_number
            WHERE ci.status = 'received'
              AND rat.notified_at IS NULL
              AND NULLIF(TRIM(ci.order_number), '') IS NOT NULL
              AND NULLIF(TRIM(c.email), '') IS NOT NULL
            ORDER BY ci.updated_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [RefundIssue(*row) for row in cursor.fetchall()]


def parse_inr(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("₹", "")
    if not text:
        return None
    amount = float(text)
    if amount < 0:
        raise RuntimeError(f"Invalid INR amount: {value}")
    return amount


def knowledge_dirs() -> list[Path]:
    configured = os.getenv("KNOWLEDGE_DIR", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(r"C:\Users\SAKSHI\SalesAI\salesai_main_worktree\salesai-email-agent\data\knowledge"),
        Path(r"C:\Users\SAKSHI\SalesAI\salesai-email-agent\data\knowledge"),
        Path(__file__).resolve().parents[1] / "salesai_main_worktree" / "salesai-email-agent" / "data" / "knowledge",
        Path(__file__).resolve().parents[1] / "salesai-email-agent" / "data" / "knowledge",
    ]
    dirs: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        if path is None or not path.is_dir():
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)
    return dirs


def load_policy_allowances() -> dict[str, float]:
    """Extract SKU rupee refund caps from RAG. Never derive a percentage in code."""
    return load_refund_policy()["allowances"]


def load_refund_policy() -> dict[str, Any]:
    """Read published Refund Allowance rupee caps and optional deduction rate from RAG."""
    allowances: dict[str, float] = {}
    deduction_rate_pct: float | None = None
    for directory in knowledge_dirs():
        for filename in ("product_catalog.md", "refund.md"):
            path = directory / filename
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            rate_match = DEDUCTION_RATE_RE.search(text)
            if rate_match:
                deduction_rate_pct = float(rate_match.group(1))
            for row in TABLE_ROW_RE.finditer(text):
                amount = parse_inr(row.group(3))
                if amount is not None:
                    allowances[row.group(1).upper()] = amount
            for block in re.split(r"\n(?=## [A-Z]+-\d+)", text):
                sku_match = SKU_HEADING_RE.search(block)
                amount_match = ALLOWANCE_RE.search(block)
                if not sku_match or not amount_match:
                    continue
                amount = parse_inr(amount_match.group(1))
                if amount is not None:
                    allowances[sku_match.group(1).upper()] = amount
    LOGGER.info(
        "Loaded %d SKU refund allowances from RAG policy documents (published deduction rate=%s)",
        len(allowances),
        f"{deduction_rate_pct}%" if deduction_rate_pct is not None else "not stated",
    )
    return {"allowances": allowances, "deduction_rate_pct": deduction_rate_pct}


def order_line_items(order_number: str) -> tuple[list[OrderLine], float]:
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(o.total_amount, 0),
                   p.sku, p.name, oi.quantity, oi.unit_price, p.price,
                   p.refund_allowance
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.id
            LEFT JOIN products p ON p.id = oi.product_id
            WHERE o.order_number = %s
            ORDER BY o.created_at DESC NULLS LAST, p.sku
            """,
            (order_number,),
        )
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(f"No order found for {order_number}")
    total = float(rows[0][0] or 0)
    lines: list[OrderLine] = []
    for row in rows:
        sku = (row[1] or "").strip().upper()
        if not sku:
            continue
        lines.append(
            OrderLine(
                sku=sku,
                name=row[2] or sku,
                quantity=max(1, int(row[3] or 1)),
                unit_price=float(row[4] or 0),
                catalog_price=float(row[5] or 0),
                refund_allowance=float(row[6]) if row[6] is not None else None,
            )
        )
    return lines, total


def match_issue_lines(issue: RefundIssue, lines: list[OrderLine]) -> list[OrderLine]:
    haystack = f"{issue.issue_title} {issue.issue_description}".lower()
    mentioned = {token.upper() for token in SKU_TOKEN_RE.findall(haystack)}
    matched = [line for line in lines if line.sku in mentioned]
    if matched:
        return matched
    named = [
        line
        for line in lines
        if line.name and line.name.lower() in haystack
    ]
    if named:
        return named
    if len(lines) == 1:
        return lines
    return lines


def resolve_refund_amount(issue: RefundIssue) -> tuple[float, dict[str, Any]]:
    """Pay the RAG-published rupee Refund Allowance. Deduction is paid minus that cap."""
    policy = load_refund_policy()
    allowances = policy["allowances"]
    lines, order_total = order_line_items(issue.order_number)
    if not lines:
        raise RuntimeError(f"No refundable line items found for order {issue.order_number}")

    selected = match_issue_lines(issue, lines)
    item_breakdown: list[dict[str, Any]] = []
    refundable = 0.0
    for line in selected:
        policy_cap = allowances.get(line.sku)
        if policy_cap is None:
            policy_cap = line.refund_allowance
        if policy_cap is None or policy_cap <= 0:
            raise RuntimeError(
                f"No RAG refund allowance found for SKU {line.sku} on order {issue.order_number}"
            )
        paid = line.unit_price * line.quantity if line.unit_price > 0 else line.catalog_price * line.quantity
        allowed = min(policy_cap * line.quantity, paid if paid > 0 else policy_cap * line.quantity)
        refundable += allowed
        item_breakdown.append(
            {
                "sku": line.sku,
                "name": line.name,
                "quantity": line.quantity,
                "paid_inr": round(paid, 2),
                "deduction_inr": round(max(paid - allowed, 0.0), 2),
                "policy_allowance_inr": policy_cap,
                "amount_inr": round(allowed, 2),
            }
        )

    amount = round(min(refundable, order_total if order_total > 0 else refundable), 2)
    if amount <= 0:
        raise RuntimeError(f"Policy refund allowance resolved to 0 for order {issue.order_number}")

    configured = os.getenv("REFUND_AMOUNT_INR", "").strip()
    if configured:
        LOGGER.warning("REFUND_AMOUNT_INR is set; policy extraction still wins unless amount cannot be resolved")

    LOGGER.info(
        "Resolved policy refund issue=%s order=%s amount=%.2f items=%s",
        issue.issue_id,
        issue.order_number,
        amount,
        item_breakdown,
    )
    return amount, {
        "order_total_inr": round(order_total, 2),
        "policy_amount_inr": amount,
        "deduction_rate_pct": policy.get("deduction_rate_pct"),
        "items": item_breakdown,
        "source": "rag_refund_allowance_rupees",
    }


class RazorpayBackend:
    def refund(self, *, order_number: str, amount_inr: float) -> dict[str, Any]:
        payment_id = f"pay_demo_{hashlib.sha256(order_number.encode()).hexdigest()[:16]}"
        if os.getenv("RAZORPAY_DEMO_MODE", "true").strip().lower() == "true":
            suffix = uuid4().hex[:12]
            return {
                "payment_id": payment_id,
                "transaction_id": f"txn_demo_{suffix}",
                "refund_id": f"rfnd_demo_{suffix}",
                "status": "processed",
                "amount_inr": amount_inr,
                "demo": True,
            }

        base_url = os.getenv("RAZORPAY_API_URL", "").rstrip("/")
        key_id = os.getenv("RAZORPAY_KEY_ID", "")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
        if not base_url or not key_id or not key_secret:
            raise RuntimeError("RAZORPAY_API_URL, RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required")

        response = requests.post(
            f"{base_url}/v1/refunds",
            auth=(key_id, key_secret),
            json={"payment_id": payment_id, "amount": round(amount_inr * 100), "notes": {"order_number": order_number}},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "payment_id": payment_id,
            "transaction_id": payload.get("acquirer_data", {}).get("arn") or payload.get("id"),
            "refund_id": payload.get("id"),
            "status": payload.get("status", "processed"),
            "amount_inr": amount_inr,
            "demo": False,
            "raw": payload,
        }


def _first_name(full_name: str) -> str:
    name = (full_name or "").strip()
    if not name:
        return "there"
    return name.split()[0].title()


def send_customer_email(issue: RefundIssue, refund: dict[str, Any]) -> bool:
    subject = f"Your refund for order {issue.order_number} has been processed"
    items = refund.get("policy_items") or (refund.get("policy") or {}).get("items") or []
    policy = refund.get("policy") or {}
    credited = float(refund.get("amount_inr") or policy.get("policy_amount_inr") or 0)
    first_item = items[0] if items else {}
    product_name = first_item.get("name") or "your returned item"
    sku_paid = float(first_item.get("paid_inr") or 0)
    deduction = float(first_item.get("deduction_inr") or max(sku_paid - credited, 0))
    greeting = f"Hi {_first_name(issue.customer_name)},"

    body = (
        f"{greeting}\n\n"
        f"Thank you for your patience. We have generated and confirmed the refund for your {product_name} "
        f"on order {issue.order_number}.\n\n"
        f"Item price: INR {sku_paid:.2f}\n"
        f"Policy deduction: INR {deduction:.2f}\n"
        f"Refund generated: INR {credited:.2f}\n"
        f"Refund ID: {refund['refund_id']}\n"
        f"Transaction ID: {refund['transaction_id']}\n\n"
        "This refund has been generated to your original payment method.\n\n"
        "If you need anything else, just reply to this email. We are happy to help.\n\n"
        "Warm regards,\n"
        "Customer Support Team\n"
        "ShopiFyX"
    )

    if os.getenv("SMTP_MOCK_MODE", "true").strip().lower() == "true":
        LOGGER.info("SMTP mock: would send refund email to %s:\n%s", issue.customer_email, body)
        return True

    sender = os.getenv("SMTP_EMAIL", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if not sender or not password:
        raise RuntimeError("SMTP_EMAIL and SMTP_PASSWORD are required when SMTP_MOCK_MODE=false")
    message = MIMEText(body)
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = issue.customer_email
    with smtplib.SMTP(os.getenv("SMTP_SERVER", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587"))) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, [issue.customer_email], message.as_string())
    return True


def claim_or_get_refund(issue: RefundIssue, amount_inr: float) -> dict[str, Any]:
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO refund_agent_transactions (
                issue_id, order_number, customer_email, amount_inr, payment_id,
                transaction_id, refund_id, gateway_status, gateway_response, notified_at
            ) VALUES (%s, %s, %s, %s, %s, 'pending', 'pending', 'processing', '{}'::jsonb, NULL)
            ON CONFLICT (order_number) DO NOTHING
            """,
            (
                issue.issue_id,
                issue.order_number,
                issue.customer_email,
                amount_inr,
                f"pay_demo_{hashlib.sha256(issue.order_number.encode()).hexdigest()[:16]}",
            ),
        )
        claimed = cursor.rowcount == 1
        cursor.execute(
            """
            SELECT payment_id, transaction_id, refund_id, gateway_status,
                   gateway_response, notified_at
            FROM refund_agent_transactions
            WHERE order_number = %s
            FOR UPDATE
            """,
            (issue.order_number,),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"Unable to claim refund issue {issue.issue_id}")
        return {
            "payment_id": row[0],
            "transaction_id": row[1],
            "refund_id": row[2],
            "status": row[3],
            "gateway_response": row[4] or {},
            "notified_at": row[5],
            "claimed": claimed,
        }


def save_gateway_result(issue: RefundIssue, refund: dict[str, Any]) -> None:
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE refund_agent_transactions
            SET payment_id = %s, transaction_id = %s, refund_id = %s,
                gateway_status = %s, gateway_response = %s, updated_at = CURRENT_TIMESTAMP
            WHERE issue_id = %s
            """,
            (
                refund["payment_id"],
                refund["transaction_id"],
                refund["refund_id"],
                refund["status"],
                json.dumps(refund),
                issue.issue_id,
            ),
        )


def mark_notified(issue: RefundIssue, refund: dict[str, Any]) -> None:
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE refund_agent_transactions
            SET notified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE issue_id = %s AND notified_at IS NULL
            """,
            (issue.issue_id,),
        )
        note = (
            f"Refund generated and confirmed for policy allowance INR {refund.get('amount_inr')}. "
            f"Refund ID: {refund['refund_id']}; Transaction ID: {refund['transaction_id']}. Issue resolved."
        )
        cursor.execute(
            """
            UPDATE customer_issues
            SET status = 'resolved',
                agent_decision = 'refund_initiated',
                suggested_action = 'Refund generated and issue resolved',
                resolution_notes = CONCAT(
                    NULLIF(resolution_notes, ''), CASE WHEN resolution_notes = '' THEN '' ELSE E'\\n' END,
                    %s
                ),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (note, issue.issue_id),
        )
        if cursor.rowcount < 1:
            raise RuntimeError(f"Failed to update customer_issues status for {issue.issue_id}")
        LOGGER.info(
            "Updated Supabase customer_issues id=%s order=%s status=resolved agent_decision=refund_initiated",
            issue.issue_id,
            issue.order_number,
        )


def _log_refund_summary(issue: RefundIssue, refund: dict[str, Any], email_sent: bool) -> None:
    items = refund.get("policy_items") or (refund.get("policy") or {}).get("items") or []
    first = items[0] if items else {}
    product_name = first.get("name") or "returned item"
    paid = float(first.get("paid_inr") or 0)
    deduction = float(first.get("deduction_inr") or 0)
    credited = float(refund.get("amount_inr") or 0)
    LOGGER.info("------------------------------------------------------------")
    LOGGER.info("REFUND GENERATED AND CONFIRMED")
    LOGGER.info("  Customer:          %s <%s>", issue.customer_name or "Customer", issue.customer_email)
    LOGGER.info("  Order:             %s", issue.order_number)
    LOGGER.info("  Product:           %s", product_name)
    LOGGER.info("  Item price:        INR %.2f", paid)
    LOGGER.info("  Policy deduction:  INR %.2f", deduction)
    LOGGER.info("  Refund amount:     INR %.2f", credited)
    LOGGER.info("  Refund ID:         %s", refund.get("refund_id"))
    LOGGER.info("  Transaction ID:    %s", refund.get("transaction_id"))
    LOGGER.info("  Payment ID:        %s", refund.get("payment_id"))
    LOGGER.info("  Gateway status:    %s", refund.get("status"))
    LOGGER.info("  Issue status:      resolved / refund_initiated")
    LOGGER.info("  Customer email:    %s", "sent" if email_sent else "not sent")
    LOGGER.info("------------------------------------------------------------")


def process_issue(issue: RefundIssue) -> bool:
    LOGGER.info(
        "Processing received return: order=%s customer=%s issue=%s",
        issue.order_number,
        issue.customer_email,
        issue.issue_id,
    )
    amount, policy = resolve_refund_amount(issue)
    first = (policy.get("items") or [{}])[0]
    LOGGER.info(
        "Policy allowance extracted: SKU=%s paid=INR %.2f deduction=INR %.2f refund=INR %.2f",
        first.get("sku"),
        float(first.get("paid_inr") or 0),
        float(first.get("deduction_inr") or 0),
        amount,
    )
    existing = claim_or_get_refund(issue, amount)
    if existing["notified_at"]:
        LOGGER.info(
            "Skipping order=%s; refund already generated and customer already notified (refund_id=%s transaction_id=%s)",
            issue.order_number,
            existing.get("refund_id"),
            existing.get("transaction_id"),
        )
        return False

    if existing["refund_id"] == "pending" and not existing["claimed"]:
        raise RuntimeError(
            f"Refund issue {issue.issue_id} is already processing; reconcile it before retrying"
        )

    if existing["refund_id"] == "pending":
        LOGGER.info("Calling payment gateway for order=%s amount=INR %.2f", issue.order_number, amount)
        refund = RazorpayBackend().refund(order_number=issue.order_number, amount_inr=amount)
        if not refund.get("refund_id") or not refund.get("transaction_id"):
            raise RuntimeError("Refund backend returned incomplete transaction identifiers")
        refund["policy_items"] = policy["items"]
        refund["policy"] = policy
        save_gateway_result(issue, refund)
        LOGGER.info(
            "Gateway confirmed order=%s refund_id=%s transaction_id=%s payment_id=%s status=%s",
            issue.order_number,
            refund["refund_id"],
            refund["transaction_id"],
            refund["payment_id"],
            refund.get("status"),
        )
    else:
        stored_policy = (existing.get("gateway_response") or {}).get("policy") or policy
        refund = {
            "payment_id": existing["payment_id"],
            "transaction_id": existing["transaction_id"],
            "refund_id": existing["refund_id"],
            "status": existing["status"],
            "amount_inr": amount,
            "policy_items": stored_policy.get("items") or policy["items"],
            "policy": stored_policy,
        }
        LOGGER.info(
            "Using saved gateway result for order=%s refund_id=%s transaction_id=%s",
            issue.order_number,
            refund["refund_id"],
            refund["transaction_id"],
        )

    email_sent = send_customer_email(issue, refund)
    LOGGER.info(
        "Customer notified: to=%s order=%s refund_id=%s transaction_id=%s",
        issue.customer_email,
        issue.order_number,
        refund["refund_id"],
        refund["transaction_id"],
    )
    mark_notified(issue, refund)
    _log_refund_summary(issue, refund, email_sent)
    return True


def run_once() -> int:
    issues = find_received_issues()
    if not issues:
        LOGGER.info("Poll: no received returns waiting for refund")
        return 0

    LOGGER.info("Poll: found %d received return(s) ready for refund", len(issues))
    processed = 0
    for issue in issues:
        try:
            if process_issue(issue):
                processed += 1
        except Exception:
            LOGGER.exception("Refund processing failed for issue=%s order=%s", issue.issue_id, issue.order_number)
    return processed


def main() -> None:
    ensure_tables()
    interval = max(5, int(os.getenv("POLL_INTERVAL_SECONDS", "30")))
    LOGGER.info("Refund agent started")
    LOGGER.info("Polling every %d seconds for customer_issues with status='received'", interval)
    LOGGER.info("Demo gateway=%s  SMTP mock=%s", os.getenv("RAZORPAY_DEMO_MODE", "true"), os.getenv("SMTP_MOCK_MODE", "true"))
    while True:
        run_once()
        time.sleep(interval)


if __name__ == "__main__":
    main()
