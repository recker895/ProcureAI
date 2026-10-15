# ProcureAI 2 acceptance evidence — 2026-10-15

## Implemented and exercised

- Existing Manual workflow retained: all 47 original backend tests pass.
- 30 additional backend tests cover HiGHS allocation, capacity/MOQ/share/budget/qualifications, exact costs, reproducibility, mode fencing, stale leases, duplicate prevention, approval permissions, overrides, invoice exceptions, partial receipts/invoices, immutable source snapshots, signed-event replay, supplier-specific orders, per-supplier matching, test-record external isolation and payment confirmation/reconciliation.
- 16 interface/theme component tests pass, including Manual controls, persisted themes, new-case defaults, case mode changes, thirteen role workspaces, truthful empty states and escaping untrusted text.
- Six real Edge browser tests pass against a disposable test server, including desktop, tablet and mobile rendering, black dark theme, Light/System behavior, persisted switches, case/stage workspaces and viewer controls.
- Runtime dependency checks pass; the recorded pip-audit result reports no known vulnerabilities in the pinned dependencies at check time.
- Live Groq check uses the actual configured model and actual HiGHS. All thirteen roles were exercised, the workflow paused at SUPPLIER_REVIEW, PO_REVIEW and FINANCE_REVIEW, and completed after isolated role-fixture decisions and TEST reconciliation. It created zero external procurement jobs. `evidence/live-ai-check.json` records its scope and observed results.
- Operational data preservation is verified in `evidence/data-preservation.json`: original users, business rows, historical versions, documents, file bytes and the existing audit prefix remain unchanged. Migration and configuration checks append system audit events.

## Evidence files

`backend-tests-v2.txt`, `ai-tests-v2.txt`, `interface-tests-v2.txt`, `browser-tests-v2.txt`, `dependency-audit-v2.json`, `live-ai-check.json`, `data-preservation.json`, `runtime-smoke-v2.json`, and the PNGs in `screenshots/`.

Browser screenshots show real rendered application screens with isolated TEST accounts/data. They are not operational procurement evidence. Provider/gateway fixtures in unit tests are explicitly test-only. Live-check approvals are role-fixture tests, not actual human verification, physical receipt, real order transmission or money movement. Earlier v1 evidence is retained and does not supersede these results.

## Remaining external requirements

SAP/ERP, email, supplier, warehouse, inspection and payment systems require actual endpoints, credentials, gateway implementations and certification. Scanned PDF/image OCR needs an OCR service or reviewed transcription. Production hosting, TLS, secret management, encrypted backups/restore drills, alerting, reviewer identities, organizational policies and independent security review remain deployment work. SSO/MFA, multi-organization tenancy, horizontal scaling and controlled posted-record amendments/returns/credit-note workflows are not claimed as implemented.

Working local behavior and these tests establish the stated scope. They do not certify unspecified external systems or a production hosting environment.
