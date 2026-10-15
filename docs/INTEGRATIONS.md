# Gateway contracts

These are the original Manual-mode contracts. ProcureAI 2 extends them with supplier-specific PO bundles, authorized RFQs, confirmed settlement, signed/polled operational events and Groq. See `AI_MODE.md` for the current contracts and deployment requirements.

Environment variables configure trusted server-side endpoints. Do not enter tokens into quotation data or source documents. Deploy behind HTTPS; adapters accept HTTPS URLs without embedded credentials and do not follow redirects.

`SAP_GATEWAY_URL` + `SAP_GATEWAY_TOKEN` enable approved PO submission. The buyer explicitly submits an approved order. A durable job calls `POST {gateway}/submit` with `Authorization: Bearer {token}` and `Idempotency-Key: {job-id}`. Body: `{operation: "purchase_order", workflow: "PR-…", po: {...approved snapshot...}}`.

The gateway must persist the key, reject a changed payload for the same key, prevent duplicate external postings, and return `{status: "confirmed", reference: "actual-external-document-id"}` only after the ERP has acknowledged issuance. A timeout, non-2xx response, or incomplete acknowledgment becomes UNKNOWN, not transmitted. Retry reuses the exact key and payload. PROCESSING jobs left by a process crash become UNKNOWN on startup. There is no blind retry with a fresh key. A confirmed external order blocks direct commercial edits.

The reference is authoritative only to the degree the organization's gateway is trustworthy. Signatures, SAP-specific mappings, production credentials, network allow-listing, service-account permissions, and ERP reconciliation must be certified by the integrating organization. Never reuse the SAP class login credentials as production API credentials.

Email/payment generic adapters have the same configured-HTTPS/acknowledgment boundary but are not wired into delivery/initiation workflows. Configuring them does not imply that email messages or bank transfers are being sent. Operational notifications currently live in the application inbox. Payment reconciliation records actual external bank evidence and a finance user's assertion; it is not automated bank verification.

Test fixtures mock gateway failures and acknowledgments solely within automated tests. The delivered runtime does not use mock responses.
