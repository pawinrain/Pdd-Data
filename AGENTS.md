# PDD Data MCP development rules

- This directory is an independent Python service. Do not modify or replace a parent TypeScript application.
- Real Pinduoduo/CDP access is permitted only after the user explicitly authorizes the current read-only stage and its dataset scope. Stage C authorization does not imply unrelated future access.
- Stage D1/D2/D3 and the implemented Stage D4 promotion baseline are complete for the newly confirmed current store. The current authorization for `st_pdd_current_01` is limited to re-checking the exact D4 `YESTERDAY` time semantics and an unnumbered post-D4 read-only extension for overall product business and traffic data. This authorization does not redefine or start Stage D5.
- Re-verify the current platform store identity for Stage D. Never reuse the prior store binding or its snapshots merely because the same dedicated Chrome is running.
- For the D4 time-semantics re-check, distinguish a complete prior natural day from prior-day same-period or another partial window, and distinguish window completion from source finalization. Preserve original evidence and committed snapshots; do not silently rewrite history.
- For the post-D4 product-business extension, keep overall product business/traffic metrics separate from the already accepted promoted-product meaning of `product_metrics`. Verify the real page, exact identifiers, fields, units, source classification, time windows, result grain, pagination, and store identity before enabling any new dataset or field. Do not present target fields as readable capabilities.
- Do not start Stage D5 host integration, AI analysis, ExecutionJob creation, unrelated datasets, or any platform business write operation.
- Connect only to a trusted loopback CDP endpoint from local configuration. Never accept a CDP endpoint through MCP parameters, launch Chrome, copy profiles/cookies, replay requests, or close a user browser.
- Keep MCP protocol messages alone on stdout while `serve` is running. Application logs go to stderr.
- Synthetic collection is allowed only when both service test mode and the configured connection opt in. Synthetic snapshots must use `source=SYNTHETIC` and a test-only data root.
- Never fabricate a successful real collection. Unimplemented or unverified adapters return explicit disabled/unverified/identity/mismatch states and never fall back to synthetic data.
- Unknown browser responses may expose only bounded sanitized metadata. Read a response body only after an exact host/path/method/content-type adapter has been reviewed and configured; never persist raw responses.
- Snapshot history is immutable. Never automatically delete committed snapshots or bypass the data-root writer lock.
- Do not accept paths, URLs, headers, credentials, or executable code through MCP tool parameters.
- Keep payloads, credentials, full URLs, and secrets out of logs.
- Preserve separation between capture time and metric-window time. Missing values stay null and truncated collections stay partial.
