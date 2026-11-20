# ShopiFyX Refund Agent

Standalone worker outside `salesai_main_worktree`. It polls PostgreSQL for
customer issues whose product/return status is explicitly `received`.

For each eligible issue it:

1. Identifies the returned SKU from the issue and order line items.
2. Extracts that SKU's rupee **Refund Allowance** from RAG policy docs (`product_catalog.md` / `refund.md`).
3. Refunds that extracted rupee cap (capped by the amount paid for the SKU). It does not apply a hardcoded percentage. Deduction = amount paid minus the RAG allowance. Never the original order basket total.
4. Calls a Razorpay-compatible refund backend.
3. Uses deterministic demo identifiers when `RAZORPAY_DEMO_MODE=true`.
4. Emails the customer that the refund has been generated and confirmed, including the amount, refund ID, and transaction ID.
5. Records the transaction in `refund_agent_transactions`.
6. Marks the open issue `resolved` with `agent_decision=refund_initiated` as soon as the gateway succeeds.

The transaction table has a unique `issue_id`, so polling retries cannot issue
the same refund twice.

## Run

```powershell
cd C:\Users\SAKSHI\SalesAI\refund-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Set SUPABASE_DB_URL and SMTP values in .env
python refund_agent.py
```

For a single poll during a demo, run:

```powershell
python -c "import refund_agent; refund_agent.ensure_tables(); print(refund_agent.run_once())"
```

## Mark a product as received

The worker intentionally does not infer receipt from delivery. An operator or
returns workflow must explicitly set the issue status to `received`:

```sql
UPDATE customer_issues
SET status = 'received',
    resolution_notes = 'Returned product physically received and inspected.',
    updated_at = CURRENT_TIMESTAMP
WHERE id = '<ISSUE_UUID>'
  AND status IN ('open', 'options_presented', 'exchange_pending');
```

Use `RAZORPAY_DEMO_MODE=true` for a safe demonstration. Set it to `false` only
when the configured backend and credentials are approved for real refunds.
