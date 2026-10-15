# ProcureAI

ProcureAI 2 extends the working application with Manual and AI modes, a shared Groq connection, real HiGHS allocation, persistent agent tasks, protected evidence, and independent human approvals. Light, Dark and System themes use neutral surfaces; Dark uses black/charcoal with emerald and violet accents. See [AI mode and upgrade guide](docs/AI_MODE.md).

## Run locally

Python 3.12+ is required. In this folder run `./start.ps1` on Windows, or:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. First use creates the administrator account. There are no default credentials or seeded operational records. The administrator creates separate buyer, procurement, warehouse, quality, and finance accounts in Settings. Sign out and sign in to exercise each role. Viewer access is read-only. One organization is supported; all authenticated organization users can read procurement records. There is no cross-organization tenancy.

## Working cycle

The following describes the preserved Manual workflow. AI mode automatically prepares and advances eligible work through the same human gates; its allocation and integration behavior is documented in `docs/AI_MODE.md`.

Buyer creates a requisition, uploads source documents, and records 3–4 quotations. Ranking checks quantity, deadline, quality, certifications, and currency before applying weighted min-max scoring. Scores, calculations, inputs, algorithm version, and sensitivity scenarios are persisted.

Buyer submits an eligible supplier choice; Procurement approves it. Buyer prepares the PO; Procurement approves it separately. Warehouse records a receipt; Quality accepts or rejects its quantity with findings. Partial deliveries and replacement of rejected quantities are supported.

Buyer records an invoice against accepted goods, runs matching, and submits it for Finance approval. Matching requires exact unit price/currency, cumulative invoice quantity no greater than accepted receipts, and a total within 0.01. Agreed freight/discount are allocated proportionally across partial invoices. Finance may return the invoice for correction. Finance records an externally executed payment with a bank reference, source attachment, and reconciliation statement. Partial payments and multiple partial invoices are supported. Manual reconciliation does not move money. AI mode can submit an independently approved request through a configured payment gateway and requires exact settlement confirmation.

Unissued commercial changes invalidate supplier and PO approvals and require a new ranking. Direct changes/cancellation after receipt, invoice, payment, or confirmed external issuance are blocked. Controlled amendments, returns and credit-note/reversal workflows are deployment backlog, not implemented UI features.

## Explanations and integrations

Manual recommendations retain their original deterministic weighted scoring. AI mode uses Groq tool calling for source-linked explanations and document intake, and HiGHS for constrained supplier allocation. Each assessment preserves its input snapshot, tool results, model, prompt version, tokens, latency, alternatives and uncertainties. Text PDFs, XLSX and plain text support extraction. Scanned images/PDFs require an OCR integration; missing capabilities are explicitly blocked.

Configured HTTPS adapters support authorized RFQs, supplier-specific PO issuance, signed/polled operational events and approved payment requests. They require the contracts in `docs/AI_MODE.md`; they are not direct connections to the SAP training site or a particular bank. Unconfigured services show Not connected. Configured services remain unverified until checked or acknowledged. In-app notifications and manual external-payment reconciliation remain available. Live ERP/email/warehouse/payment endpoints, credentials and integration certification are deployment requirements.

## Verification

```sh
python -m unittest discover -s tests -v
```

Tests use isolated temporary databases and fictional records; they do not modify operational data. See `docs/ACCEPTANCE.md` for executed results and remaining deployment requirements. REST API documentation is available at `/api/docs` in local mode; schema at `/api/openapi.json`.

Frontend tests require Node.js and test-only dependencies. In `tests/` run `npm install` and `npm test`. Browser tests use `ops/serve_ui_test.py` with a disposable database, then `npm run test:browser` (Edge channel; change the configuration for Chromium if required). Runtime uses native browser JavaScript and needs no frontend compilation. Browser screenshots and test reports are included in `evidence/`; they depict isolated test records. `ops/live_ai_check.py report.json` is an opt-in live Groq check that consumes provider tokens, uses a disposable TEST database, and exercises approval gates through role fixtures rather than claiming actual human review.

## Deployment boundary

The supplied container/proxy configuration targets a single application instance on local disk. SQLite serializes writes; do not place its data directory on OneDrive, SMB, NFS, or a network share. Multi-instance/high-write deployments require a PostgreSQL migration and shared document/object storage implementation.

See `docs/OPERATIONS.md`, `docs/INTEGRATIONS.md`, and `docs/ARCHITECTURE.md`. Live infrastructure, organizational policy sign-off, independent security review, restore verification in the target environment, and integration certification are required before operational deployment. Local functionality and automated tests do not establish these external readiness conditions.
