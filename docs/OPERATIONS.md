# Operating ProcureAI

## Deployment

Copy `.env.example` to `.env`, configure a real domain, HTTPS PUBLIC_ORIGIN and ALLOWED_HOSTS, and replace BOOTSTRAP_TOKEN with a cryptographically random secret. Configure DNS, then run `docker compose up --build -d`. Caddy provides TLS termination. Bootstrap is one-time and disabled after the first account exists. Store secrets using the deployment platform's secret manager; do not commit .env. Remove the bootstrap token after provisioning. The container runs as non-root with a read-only application filesystem and a writable data volume.

Production cookies are Secure, HttpOnly, SameSite=Strict. Mutations require a session-bound CSRF token and matching Origin. Sessions expire after 8 hours. Passwords use salted scrypt; there are no default passwords. Repeated failed logins are limited by account/client combination. Password changes and user deactivation revoke sessions. RBAC and separation of duties are enforced on the server. MFA, SSO, password-reset delivery, record-level team scoping, and organizational retention policies need integration before broader deployment.

The environment is single organization, single application instance. Proxy/body limits, firewall rules, trust settings, expected workload, and endpoint access should be reviewed in the target environment. Audit is append-only through application/database triggers and hash chaining, not an independently protected WORM archive: a database administrator could rebuild it. Export and sign audit checkpoints to independent storage if tamper resistance against administrators is required.

## Monitoring

Probe `/health/live` and `/health/ready`. HTTP information is logged (method, path, status, duration); secrets and request bodies are not logged. Monitor 5xx responses, latency, disk space, locked-database failures, QUEUED/RUNNING/RETRY/BLOCKED agent tasks, pending/UNKNOWN integration jobs, approvals and invoice exceptions. `/api/automation` reports task queues, token totals, latency and estimated cost when token prices are configured. Notifications are role-addressed in-app. External email requires an enabled communication policy, a configured gateway and an exact recipient allowlist.

## Backup and restore

Set PROCURE_DATA to the deployed data directory when running maintenance commands. `python ops/backup.py /secure/path/backup.zip` creates an online SQLite snapshot and includes the exact document set referenced by it, validating hashes. Store encrypted copies outside the application host with organization-defined retention. Backup contains password hashes, sessions, and business evidence; restrict access.

Run `python ops/restore.py backup.zip /new/empty/directory`. It validates database integrity, document hashes, archive paths, and audit chain. Stop the app, switch PROCURE_DATA to the restored directory, revoke restored sessions (`DELETE FROM sessions` through a maintenance connection), restart, and confirm login, documents, workflow counts and audit integrity. Do not overwrite an active data directory. Run a scheduled restore drill; define RPO/RTO with the organization.

## Unknown external outcome

Keep the job key and original payload. Check the gateway/ERP for that key before retrying. The retry endpoint reuses the same key. Do not create a new workflow or externally post a second order. If the gateway cannot reconcile by idempotency key, stop and resolve with its operator. A startup-recovered PROCESSING job is UNKNOWN for this reason.

## Updates

Backup, test restored data in staging, review migrations, deploy a single version, and run readiness and acceptance checks. The runner applies 001 and numbered migration 002; 002 adds case modes, isolated record classifications, agent tasks, immutable assessments, intake proposals, signed-event deduplication and connection status. Existing cases receive Manual mode; previously named TEST cases are classified as test and other legacy records require administrator classification before external execution. Original business snapshots are not rewritten. Rollback uses the pre-upgrade database and source backups in a new empty data directory; archive the current state first. See `AI_MODE.md`.
