# Channel3 and Kernel Shopping Demo Implementation Plan

**Goal:** Discover real products, inspect a source listing in a Kernel browser, and prepare a clearly labelled Countersign sandbox purchase for human confirmation.

**Architecture:** Authenticated seller UI and agent MCP tools share shopping functions. Channel3 results are normalized and stored as owner-scoped, immutable discovery snapshots. A separate sandbox SKU copies the snapshot's USD price and known attributes; existing mandate validation, confirmation and PayPal payment remain authoritative. Browser observations are supplementary, not verified retailer facts or proof of fulfillment.

**Tech stack:** Existing FastAPI, httpx, Postgres, MCP, Next.js and Tailwind; no new dependencies.

## Scope and acceptance

- [x] Add optional backend-only provider settings and a sandbox/demo gate. Test disabled/live mode and anonymous access.
- [x] Test provider contracts with httpx MockTransport: valid results, missing attributes, invalid prices/URLs, provider failures, Kernel execution failure and session cleanup. Implement the smallest shared provider client.
- [x] Store discovery candidates with owner ID and source timestamp. Test ownership, over-cap and wrong-attribute rejection, sandbox product provenance in receipt events, and absence of payment side effects. Add seller routes and buyer MCP wrappers.
- [x] Add `/shop` inside the existing seller shell. User chose chat beside product cards/browser; implemented actual LLM tools, owner-checked numbered follow-up context, Kernel view with separate-viewer fallback, stop browser, sandbox disclosure, cap/attribute controls, readable violations and human confirmation link. Read installed Next.js docs; type-check/lint pass.
- [x] Extend the existing buyer agent policy for optional external discovery, requiring truthful sandbox disclosure and no automated confirmation/payment. Document environment setup, demo steps, provider limits and live verification.
- [x] Run targeted tests first, full backend suite (169 pass), frontend checks, real provider smoke calls and Brave UI walkthrough. Preserve previous uncommitted capture fix and documentation. Embedded video initially blank in Brave; separate viewer verified. Source security checks remain a demo constraint.

## Verification commands (PowerShell, backend directory)

`& 'C:\Users\Bhuvansai\.venvs\countersign-backend\Scripts\python.exe' -m pytest tests/test_shopping.py -q`

`& 'C:\Users\Bhuvansai\.venvs\countersign-backend\Scripts\python.exe' -m pytest -q`

Frontend: `node node_modules/typescript/bin/tsc --noEmit` and `node node_modules/eslint/bin/eslint.js .`.

No publishing, account/key creation, new wallet integration or automated PayPal approval is part of this change.
