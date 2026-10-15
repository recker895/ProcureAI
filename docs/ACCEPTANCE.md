# Acceptance report — 14 October 2026

## Executed verification

- 47 backend/domain/API tests passed in a fresh isolated test run (167.826 seconds). No failed or skipped tests.
- 13 frontend/component/theme tests passed (6 theme + 7 isolated DOM tests). These are not browser rendering or layout tests.
- JavaScript syntax check passed; Python modules compiled successfully; pip dependency consistency check passed.
- Dependency advisory scan of the pinned runtime requirements reported no known vulnerabilities at scan time. This is not a security certification.
- Online database/document backup, archive integrity, restore into a fresh directory, document checksums, and audit chain verification passed in the automated suite.
- The running local application returns a ready health status and serves its application assets. Operational database contains no sample records or default accounts; first-run administrator setup remains available.

Backend coverage includes the complete approved cycle; stable supplier ranking, eligibility and currency constraints; sensitivity snapshots; mandatory supplier count; invalid input; supplier override reason; server RBAC and self-approval prohibition; CSRF/origin checks; logout, password/session revocation, user deactivation and login throttling; persisted preferences; duplicate and concurrent submissions; stale writes; approval invalidation; blocked posted-record edits; partial/replacement receipts; duplicate receipts/invoices; three-way match and correction; configurable total tolerance requiring reapproval; partial invoices and payments; attachment authentication/signatures/checksums; append-only audit; truthful dashboard aggregation; disconnected integrations; timeout recovery/retry with the original key; and blocking edits during unknown external submission.

Frontend coverage includes empty dashboard/navigation rendering, backend preference submission with CSRF, no business mutation on theme change, quotation/inspection-stage workspace structure, read-only role controls, escaped untrusted text, truthful disconnected-service labels, and record search. Theme tests exercise explicit/system preference, initialization, storage denial, and semantic palette coverage.

## Visual verification limitation

The in-app browser automation tool repeatedly failed before initialization with “failed to write kernel assets: The system cannot find the path specified.” Reset and retry did not recover it. The normal Codex browser preview was requested, but no screenshot or live browser interaction could be verified. Desktop/mobile layout, chart rendering, native browser keyboard behavior, focus trapping, and rendered Light/Dark visual comparison therefore remain unverified. No mock screenshot is supplied as application evidence.

## Implemented runtime

Persistent authentication, organization roles, source-document upload/download, versioned requisitions and records, ranking/rule explanations, supplier and PO approval gates, warehouse receipts, independent quality disposition, blocked invoice correction/matching, finance approvals, externally executed payment reconciliation, stored notifications, audit/versions, dashboard analytics, theme preferences, global search, and explicit SAP-gateway submission jobs are implemented.

## Outstanding deployment/integration work

- Configure and certify a real SAP gateway, endpoints and credentials. No live SAP transmission was performed.
- External email delivery and bank/payment initiation are not wired. In-app notifications and evidence-backed external payment recording are available; payment execution and automated bank verification are not claimed.
- LLM narration and PDF/OCR extraction are not configured. Deterministic explanations and reviewed structured quotation entry work.
- Amendments to externally issued orders, returns, credit notes, accounting reversals, controlled exception approvals, formal short-closure, and automatic due-date scheduling require additional workflows. The application blocks direct rewrites/cancellation of already-posted records rather than corrupting their history.
- Configure production domain/TLS, protected secret storage, organization policies, monitoring/alerts, backup retention and target-environment restore drills. The container/proxy configuration has not been built or deployed in this environment.
- Complete independent security/accessibility review, MFA/SSO and password-reset delivery where required, malware scanning for attachments, and any record/team visibility restrictions. Current users share one organization read scope.
- Load/performance tests and PostgreSQL/shared object storage migration are required for multi-instance/high-write deployments. Supplied runtime targets one application instance with SQLite on local disk.

This is an implemented local application with verified business/API behavior. It is not declared certified for live organizational use until the above applicable requirements are resolved.
