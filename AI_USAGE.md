# AI Usage Disclosure

ChatGPT was used as an engineering assistant throughout this assignment. It helped reason about the durable state-machine design, draft and revise implementation code, propose regression tests, inspect PostgreSQL concurrency behavior, and debug failures observed during local test and mock-provider runs. AI assistance was used for implementation and debugging, not only for documentation.

I ran the setup, test, provider, database, and recovery commands locally. I supplied the actual command output and provider/database reports during debugging, reviewed proposed changes, and ran the final verification commands against the supplied mock provider. I am responsible for understanding and explaining the submitted implementation and its trade-offs.

## Representative prompt examples

The following are representative paraphrases of prompts used during development, rather than claimed verbatim quotations:

1. **Requirements and edge-case review:** Review the durable dispatcher against R1–R9. Identify gaps in calling-window enforcement, shared tenant concurrency, one-active-call-per-phone, attempt counting, retry/callback priority, global DND, webhook idempotency, and final-state precedence. Recommend focused regression tests before changing code.
2. **PostgreSQL concurrency debugging:** Analyze the observed PostgreSQL deadlock between webhook handling and provider-result reconciliation. Trace the conflicting row/foreign-key locks, identify a consistent lock order, and suggest a transaction-safe fix that does not put provider side effects inside retryable database transactions.
3. **Live recovery diagnosis:** Compare `/admin/report`, `/admin/leads`, dispatcher `/status`, and PostgreSQL attempt records after an interrupted run. Determine whether the provider and database belong to the same simulated session, and design a safe kill/restart test that reuses the same provider process and database without double-dialling.

## Development and review approach

Work proceeded iteratively: clarify the state transitions and invariants; implement persistence and provider integration; add unit and PostgreSQL-backed tests; run the unchanged mock provider; inspect real HTTP, SQL, and verifier output; then make targeted fixes for observed defects. The final checks included a clean 200-lead run, a 200-lead failure-injection run, and a focused one-lead process-kill/restart smoke test.

AI-generated suggestions were not treated as proof of correctness. Results and claims in `SUBMISSION.md` and `verification-evidence/VERIFICATION_RESULTS.md` are limited to the tests and live runs actually performed. The full unseen-seed and repeated arbitrary crash timings used by an external evaluator remain independent verification responsibilities.
