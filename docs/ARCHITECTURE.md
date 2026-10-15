# Architecture and rules

This file describes the preserved Manual rule engine. ProcureAI 2 adds allocation-aware business rules and durable Groq agent execution; `AI_MODE.md` is the authoritative description of the new mode, provenance and adapter boundaries.

FastAPI handles authenticated REST requests. Native browser JavaScript uses same-origin APIs; no client-side authorization substitutes for server checks. SQLite stores user accounts, hashed session tokens, case snapshots, immutable versions, source-document metadata, audit chain, notifications, integration jobs, and idempotency results. Files are stored using random IDs outside static assets and downloaded only after authentication.

Every business write uses BEGIN IMMEDIATE, an expected case version, and a per-user idempotency key with a request digest. A replay of the same key/body returns its original result; a changed body is rejected. Concurrent writes serialize, and the stale writer receives 409. Version snapshots persist all prior records and decisions.

The rule engine uses Decimal for amounts. Currency conversion is not inferred: differently denominated quotations are excluded until a reviewed same-currency quotation is supplied. The ranking formula is min-max normalization among eligible suppliers, reversed for cost, delivery and risk, with configurable weights totaling 100. Equal metrics receive a normalized 1 for all candidates. Ties resolve by cost, supplier name, then quotation ID. Sensitivity snapshots transfer 10 percentage points between cost and each other factor. No LLM can override these calculations.

Key transitions: QUOTATIONS → SUPPLIER_REVIEW → SUPPLIER_APPROVED → PO_REVIEW → PO_APPROVED → QUALITY → RECEIVING/INVOICE → INVOICE_MATCHING → MATCHED → FINANCE_REVIEW → PAYMENT_PENDING → COMPLETED. Partial invoices/payments may return to RECEIVING or INVOICE until the requirement and payable are fully settled. Blocked invoices require a versioned correction and new matching. Commercial changes before posting return to QUOTATIONS and clear dependent approvals. Posted or confirmed issued records are preserved and direct edits are blocked.

Approval snapshots bind to commercial version, not the incidental case version changed by later warehouse events. Draft submitter and approver identities must differ. Roles are single-valued to enforce procurement/finance separation. Audit actor types distinguish human, rules-engine agent, and external gateway events.

Dashboard aggregates use approved PO snapshots and entered invoices, all time, per selected currency. Counts cover all records in the single organization. Trends group by calendar month. No percentage growth is displayed without a baseline. Activity comes from audit events, not fabricated example rows. Preference changes do not mutate procurement cases.
