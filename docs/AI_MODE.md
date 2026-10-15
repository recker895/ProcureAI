# ProcureAI 2: AI mode, contracts and operations

## Modes and existing records

Manual uses the original calculations, actions and approval gates. Presentation is redesigned. The header changes the user's default for NEW cases outside a case; inside a case it opens a reason-required, version-checked transition. Every case persists its own mode. Default preferences do not change existing cases.

Mode changes increment a fencing epoch. Queued tasks and queued AI external jobs are cancelled; results computed under an earlier epoch cannot mutate the case. Processing external operations remain recorded and must be reconciled. Confirmed payment settlements can still reconcile a previously authorized request after a switch to Manual.

Migration 002 preserves users, sessions, business records, document files, original versions and audit history. Existing cases default to Manual. Existing titles beginning TEST are marked test; other existing records are legacy and require administrator classification before live external actions. New cases explicitly choose operational or test. Test/legacy records cannot queue live messages, orders or payments.

## Agent execution

Thirteen on-demand roles: coordinator; requisition; document intake; supplier communication; supplier risk; optimization; PO; delivery monitoring; goods receipt; quality; invoice matching; finance preparation; reconciliation. These are scoped worker roles, not thirteen independently deployed services.

The durable queue is implemented in SQLite. Task intents are unique per case, role, input intent and mode epoch. Two independent tasks can run concurrently; Groq requests share a rate limiter. Network calls occur outside database write transactions. Tasks use leases, bounded provider retries (three attempts), backoff and restart recovery. Stale input snapshots and mode epochs are revalidated before applying any result. Deployment requires ONE app process/instance on local storage; horizontal scaling requires a PostgreSQL/shared-storage migration, not merely more Uvicorn workers.

Groq uses scoped local tools. Calculators must run before an assessment is accepted; citations must reference available case/document IDs. Agents cannot call approval actions or arbitrary network, SQL or shell tools. The coordinator and LLM narrative cannot override backend rules. Numerical computation is deterministic; language-model phrasing is not guaranteed to reproduce word for word.

Agents perform extraction, allocation, PO preparation, matching and finance submission automatically when inputs and prerequisites are valid. Approvers decide supplier allocation, exact PO terms, and specific payment proposals. Physical receipt and quality need an accountable human or authenticated operational source. Expected approvals do not mean the order or payment was executed.

Each immutable `agent_runs` row stores original case/evidence inputs, tool arguments/results, explanation, model, prompt version, tokens, elapsed time and optional cost estimate. Explanations show reasons, alternatives, uncertainty and next action. Original deterministic Manual explanations remain accessible. A decision rationale is not private chain-of-thought or calibrated probability confidence.

## Optimization and supplier-specific orders

HiGHS MIP quantities are integers in thousandths. Capacity, MOQ, delivery, qualifications, currency, total demand, budget, supplier count and maximum share are enforced. Objectives are landed cost or landed cost plus explicit risk/quality penalties in currency per point per unit. Freight/discount/tax are calculated from approved terms. Decimal calculations verify returned quantities and rounded line totals. Infeasible outcomes block progression; non-optimal feasible results require explicit review. Solver version, configuration version, input snapshot, solver termination/gap, objective and alternatives are preserved.

One allocated supplier creates one PO; multiple suppliers create separate supplier-specific orders inside one approval bundle. The gateway must map those orders to distinct external PO references. Receipt and invoice inputs select the relevant supplier allocation. Matching and accepted quantities are checked per allocation, including partial deliveries and invoices. Direct amendments of posted/issued records remain blocked; controlled returns, credits and amendments are a documented backlog.

## Source intake

Upload a source file, choosing quotation, invoice, receipt or inspection. Plain text, text PDFs (up to 100 pages, first 30 extracted), and XLSX (bounded rows/sheets/expanded size) are supported. Files are hashed and protected by authentication. Source text is bounded; large files may require splitting. Groq extracts structured fields and exact excerpts; the server checks excerpt membership and numeric support. Complete valid quotation/invoice inputs are applied; missing fields or invalid transitions remain reviewable proposals. Reviewers enter verified missing values in forms. Receipt/inspection proposals cannot fabricate physical facts.

Image OCR and scanned-PDF OCR are currently not connected. They produce explicit errors; no simulated extraction is used. An OCR adapter or reviewed transcription is required for those formats. Model-extracted field meanings remain interpretations and require the normal human business approvals; excerpt matching cannot establish semantic truth.

## Configure Groq

Set `GROQ_API_KEY` and `GROQ_MODEL` on the server or in private `.env`. Environment variables take precedence. Restart after changing configuration. Settings > Groq AI > Test connection is administrator-only and checks model availability using the actual provider. Keys are not returned to the browser, logged, or included in source ZIPs. Use secret management in production. Rotate a key that has been disclosed in chat.

The tested account's token limit is discovered from response headers. Requests use role-specific contexts, bounded tool loops, shared pacing and retry-after handling. Provider rate limits can delay work. Increasing the agent count does not create more provider capacity. Token costs remain unknown unless configured input/output prices are supplied; reported estimates are not billing statements.

## Adapter contracts

Adapters use configured HTTPS base URLs without embedded credentials and bearer tokens. Unconfigured adapters visibly show Not connected. They are custom contracts, not guessed native SAP/bank APIs. Implement and certify each adapter against its actual system.

`POST {BASE}/submit` receives an operation payload plus `Idempotency-Key`. The gateway MUST enforce idempotency across retries and support reconciliation by that key. A valid acknowledgment is `{ "status": "confirmed", "reference": "external-reference" }`. Timeout, malformed response, unconfirmed result or interrupted processing becomes UNKNOWN. UNKNOWN external actions are not automatically replayed; a buyer can explicitly reconcile/retry the original key, at most three attempts. Commercial changes are blocked while order/payment outcomes are pending or unknown.

SAP payload: `{operation: "purchase_order" | "purchase_order_bundle", workflow, po}`. For a bundle the acknowledgment also requires `orders: [{id: "internal-supplier-order-id", reference: "external-PO-number"}]` for EVERY supplier order. A bundle acknowledgment without these mappings does not establish transmission.

Email payload: `{operation: "rfq", workflow, recipients, subject, body, reminder}`. `EMAIL_AUTOMATION_ENABLED=true`, `EMAIL_ALLOWED_RECIPIENTS` and authorized case supplier contacts are all required. Missing quotations can receive bounded follow-ups according to EMAIL_REMINDER_DAYS/EMAIL_MAX_REMINDERS. Source communication does not authorize negotiating or accepting changed commercial terms.

Payment payload: `{operation: "payment", workflow, invoice_id, amount, currency, payee_reference, finance_approval}`. The specific invoice, amount, approved beneficiary reference and commercial version are checked before execution. Current AI policy sends the approved request immediately; Finance must approve at the intended release time. Automatic due-date scheduling is not implemented. Confirmation requires `settled: true`, matching `invoice_id`, `amount`, `currency`, `payee_reference`, plus `reference` and ISO settlement `date`. A generic acknowledgment does not mark paid. Exact confirmed settlement is stored as evidence and reconciled. Missing beneficiary/integration data routes to manual reconciliation or reviewed correction.

`GET {BASE}/events?workflow=...` can return `{events: [...]}` from configured supplier/warehouse/quality/payment adapters. Polling is bounded and deduplicates event IDs. External notifications may alternatively call the signed endpoints below.

`POST /api/integration-events/{supplier|warehouse|quality|payment}` requires X-Event-Timestamp (Unix seconds) within five minutes and X-Event-Signature = hexadecimal HMAC-SHA256 over `timestamp + '.' + raw_body`, using that integration's WEBHOOK_SECRET. The body is `{event_id, workflow, operation, fields, confirmed}`. Supplier operations: quotation/invoice; warehouse: receipt; quality: inspection. Warehouse/quality require `confirmed:true`. Fields follow the existing action schemas. Events cannot cross case evidence boundaries or approval gates.

Payment settlement additionally requires `settled:true`, `invoice_id`, `job_id`, fields containing currency/amount/reference/date/payee_reference, and an existing matching approved request. Duplicate event IDs replay only identical bodies. Different bodies under the same ID are rejected. Physical/financial results are attributed to external systems, not to AI inference.

## Deployment, backup and rollback

Use the supplied single-instance Docker/Caddy deployment with HTTPS, server secrets and persistent local-disk data. Runtime versions are pinned. Health probes, authenticated automation metrics, task/job status and audit events provide operational visibility. Configure external monitoring, encrypted backup storage, retention and restore drills in the deployment environment.

Run `ops/backup.py` with PROCURE_DATA set to the intended database. Backups include the full new schema and referenced document set. Restore into a NEW empty directory with `ops/restore.py`; verify audit and file hashes before switching. Pre-upgrade backups were saved privately in the workspace `work/` directory. To roll back, archive current data, restore the pre-upgrade backup into a new directory and run the pre-upgrade source against it. Do not point old code at new allocation records. No destructive automatic downgrade is provided.

Outstanding deployment requirements: actual ERP/email/supplier/warehouse/inspection/payment endpoints and credentials; gateway contract certification; OCR for scanned files; target-host provisioning/TLS/backup monitoring; organizational reviewer identities/policies; security and restore validation in the deployment environment. Existing single-organization read access, lack of SSO/MFA/password reset and administrative audit limitations remain documented. These are not represented as completed integrations.

## Verification scope

Backend tests use disposable databases; provider/gateway fixtures are labelled TEST and never operational successes. Browser tests run against a separate disposable server at 8766 with fresh browser contexts, never an existing browser profile. The optional live Groq check uses actual Groq and HiGHS with synthetic TEST documents in a disposable database, while role fixtures exercise human gates. It does not establish actual human review, physical receipt, real SAP transmission or money movement. Executed results and limitations are recorded in `evidence/` and `docs/ACCEPTANCE_V2.md`.

## Single-window AI POC console

An AI Test case created by the signed-in buyer exposes **Process next**. Remain in the same case and account: supplier, PO and finance decisions open their existing review dialogs; receipt, quality and invoice dialogs propose editable Test values from approved records. Agents continue preparing work between checkpoints; existing agent polling updates the same window. Upload actual quotation sources or enter clearly labelled Test quotations first. Missing source data and infeasible optimization still require correction.

The creator buyer can exercise stage personas only through the authenticated POC API marker, which the server validates against the case mode, Test classification and creator identity. Manual and operational authorization rules stay unchanged. Every console action stores the actual user and explicit single-user POC attribution. This demonstrates approval UI; it is not evidence of independent employee approval. POC-reviewed cases cannot be promoted to operational records. A separate operational case is required.

Test payment completion generates a clearly labelled Test evidence file and records **Simulated test payment; no money transferred**. It never calls an external gateway. Warehouse, inspection and invoice Test entries are demonstration facts, not verified operational events.

Verified: 55 automation/backend tests (including inherited regression cases) and seven browser scenarios. The new browser scenario traverses every human checkpoint with one buyer login. Its isolated server disables the worker and advances agent-prepared stages through validated APIs; it does not claim a fresh live Groq end-to-end run. Earlier live Groq verification remains separately documented.

## Model routing and capacity handling (15 October 2026)

Document intake now prefers `GROQ_INTAKE_MODEL=openai/gpt-oss-20b`. Reviews prefer `GROQ_MODEL=openai/gpt-oss-120b`, with `GROQ_FALLBACK_MODEL=openai/gpt-oss-20b` when the preferred model is rate-limited or temporarily unavailable. Intake can fall back to the primary review model. Availability was checked against this account's models endpoint. The final model and any fallback reasons are retained with the immutable tool results. Schema, source, business-state and approval checks still apply equally to either model. Fallback is a best-effort use of configured account capacity, not a guarantee that a different model has remaining quota.

Structured 429 diagnostics identify the limit type only when supplied by Groq (TPM/TPD/RPM/RPD/ITPM/OTPM), preserve reported limit/used/requested counts and the actual retry timing, and omit organization identifiers. An unknown type stays unknown. Local token pacing is explicitly labelled LOCAL_TPM_PACING. Daily waits are no longer clamped to five minutes. Capacity waits do not consume the bounded validation-failure attempt budget. A cooldown prevents repeated requests to a limited model; if all configured models are unavailable, the durable task remains scheduled. This is single-process pacing; multiple deployments need shared capacity coordination.

The UI exposes retry eligibility countdowns and routing configuration. A zero countdown means eligible for the worker, not guaranteed provider completion. Source upload uses a content hash scoped to the workflow and returns the existing document for an identical file. Repeated extraction requests reuse the existing intake. Pre-existing duplicate files are retained and marked Duplicate; their redundant extraction tasks are skipped with an audit event. Identical files in separate cases remain distinct.

The backend now runs its deterministic calculator without an LLM round trip and supplies its verified output to the explanation tool. Routine stages normally need one AI call; document extraction normally needs two. Source-confirmed extraction checkpoints survive a capacity wait, so the next attempt can finish the explanation without repeating extraction. Requirement review is keyed to actual requirements, not every quotation's commercial version; supplier risk analysis waits for at least three quotations. No approval is automated by these changes.

Verification: the full regression run passed 111 tests; subsequent targeted runs passed 55 automation/POC tests and 10 provider/duplicate/recovery tests. Component/theme tests passed 17 cases and seven browser scenarios passed. Live 20B extraction validated Brookfield's synthetic source with 2,441 input and 414 output tokens in two provider calls. These counts describe that specific check, not future usage. The operationally stored Test case has all three saved supplier quotations; duplicate Brookfield remains attached but skipped. Provider rate limits may still pause later agent stages.

## Guided AI workspace (15 October 2026)
AI Test cases owned by the signed-in Buyer now offer Open guided workflow. The persistent dialog shows stage progress, saved agent recommendations, visible rationale, alternatives, uncertainty and evidence beside the current human checkpoint. Saving a checkpoint fetches the authoritative case and advances in the same dialog. Between checkpoints, read-only polling refreshes every five seconds. Forms are not polled or overwritten while editing. Approvals still require an explicit submission with a reason. Closing the workspace does not save an unfinished form. Completed cases show a completion summary and label prior assessments as historical. Manual flows and server authorization remain unchanged.
Validation: 20 UI/theme component tests passed, including no automatic approval and same-dialog continuation after explicit human submission. No live procurement record was altered by these tests.

Citation recovery: invalid assessment source IDs are rejected with explicit allowed-ID feedback within the existing five-turn budget. Repeated invalid citations still fail closed. Guided recovery now covers all blocked agents, including optimization. Validated with 12 provider tests and 15 UI tests; no live approvals performed.
