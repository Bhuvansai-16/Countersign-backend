# Shopping chat demo: Channel3, Kernel and Countersign

Open `http://localhost:3000`, then **Open app**. Shopping needs no login; merchant pages retain their separate login. The shopping agent chats beside real Channel3 product cards, with purchase details and optional Kernel browser inspection after selection. It uses the existing configured LLM; provider keys remain on the backend. A signed, HttpOnly visitor cookie isolates each shopper's saved candidates. These routes remain disabled outside enabled sandbox mode.

## Setup

Add these to the ignored backend `.env`, then restart the backend using `RUN_LOCAL.md`:

```dotenv
DEMO_ENABLED=true
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
CHANNEL3_API_KEY=your-private-key
KERNEL_API_KEY=your-private-key
```

Existing `LLM_*`, database and merchant settings are also required. Schema startup adds `shopping_candidates` without changing existing rows. No new packages are needed. Seller endpoints require login; shopping functions are disabled outside enabled sandbox mode. MCP candidates belong to the authenticated agent.

## Demo sequence

1. Enter the total budget and optional required colour/material. Ask the chat to find a product. The visible controls govern checkout; update them explicitly when the request changes.
2. Review real Channel3 results and exact known attributes. Unknown stays unknown. “Sage” and “Shadow Sage Green” differ under the strict validator; a missing material does not establish linen.
3. Select a product, then **Inspect source listing**. Watch the source page load beside the conversation; use **Open separate viewer** if the embedded viewer stays blank. Retailer pages may block access or require consent. A browser observation does not verify retailer facts, stock or fulfillment. Recognized security-check text is recorded as blocked and returned as an inspection error, never solved automatically.
4. Demonstrate a blocked cart by setting a deliberately wrong colour or lowering the cap below item price plus $6, then choosing **Prepare purchase**. Read the actual violations.
5. Restore a sufficient cap and matching known attributes. For a missing attribute, choose another product or explicitly remove that requirement; never claim it was verified. Prepare the cart again.
6. Open **Review purchase**. The human reviews the sandbox-copy name, seller and total, then confirms and approves the PayPal sandbox payment. The agent cannot confirm or pay. After capture, the original shopping tab verifies payment and acknowledges it; checkout closes when permitted, otherwise returns to `/shop?paid=<receipt>`.
7. Show the existing receipt verification page and matching PayPal commitment. Discovery provenance is in the receipt hash chain before confirmation seals it. Browser text is supplementary context, not cryptographic proof that a retailer statement is true.
8. **Stop browser** when finished. The 300-second timeout measures inactivity, including live-view activity; it is not a maximum session duration. An open live view can keep accruing usage.

## Sandbox boundary

Channel3 discovery does not grant control over another retailer's checkout. Preparing a purchase creates a separate `[SANDBOX COPY]` SKU at Maya's Loom using the saved source price/attributes. The PayPal sandbox seller receives the payment; no order is placed at the source retailer. Demo shipping is fixed at $6 and does not represent that retailer's shipping or taxes. These demo SKUs use zero cost until a seller supplies genuine economics; do not represent their dispute economics as real retailer margins.

The chat calls server tools with the UI's fixed budget and attributes; model tool arguments cannot raise the cap. Product buttons provide a direct way to continue if the model is slow. Search returns up to six eligible offers; chat is bounded to four model rounds with 90 seconds per model call. Provider failure never silently substitutes fake results. Source text is untrusted, never executable instructions.

## Existing MCP buyer agent

With the sandbox demo enabled, four additional tools are available: `search_external_products`, `inspect_external_product`, `stop_external_browser`, `create_external_mandate`. The existing buyer script supports explicit external-discovery requests, for example:

> Use Channel3 to find a linen throw under 90 USD including sandbox shipping. Inspect a chosen candidate with Kernel, prepare a clearly labelled sandbox copy, give me the human confirmation link, and stop the browser when finished.

Use the buyer-agent command in `RUN_LOCAL.md`, replacing its instruction. Keep API keys and payment credentials out of chat and retailer pages. Live-view links grant access to an isolated session; do not publish them as permanent links.

## Verification — November 6, 2026

171 backend tests passed, 11 live LLM evaluations deselected; shopping-only rerun after the copy adjustment: 16 passed. Visitor tests cover isolated candidates, signed/expired cookies, merchant authorization rejection and Origin checks. Frontend production build, TypeScript, ESLint and seven Node tests pass. A fresh anonymous browser returned six real products and prepared an $80.97 unconfirmed quote. Desktop and 390px mobile layouts have no horizontal overflow. Long chat replies expand through More details; all original response text remains accessible. Existing captured receipt return, cross-tab acknowledgement, script-opened tab close and browser-blocked redirect were verified without another payment.

## Earlier verification — November 5, 2026

- **169 backend tests passed**, 11 existing live LLM evaluations deselected. New coverage includes provider contracts, invalid offers, ownership, sandbox gates, fixed chat budget, numbered follow-up context, provenance, Kernel execution failures, security-check handling and idempotent cleanup.
- TypeScript and ESLint passed.
- Real Channel3 search returned four candidates. A separate live application search returned six.
- Real Kernel execution opened a source link and returned its retailer title. A live-view URL was present; the test browser was deleted afterward.
- Running API rejected `ATTRIBUTE_MISMATCH`, then prepared an $86 cart in `PENDING_CONFIRMATION` with no PayPal order.
- Brave UI: real chat search produced product cards. The cart button rejected a colour mismatch and missing material. After explicitly changing the controls and requesting product 1 as a pillow, the live chat called `prepare_purchase` and returned an $85.95 sandbox quote with the human confirmation link.
- Chat mandate `rcpt_6ee09dc4ef0996cbef375500` was subsequently paid by the owner ($85.95). November 6 read-only browser checks confirmed `CAPTURED`, verified paid return, cross-tab acknowledgement, script-opened checkout close and redirect when closing was blocked. No additional payment was made during these checks.
- Kernel's standalone live video rendered. The tested source buy link displayed a security check. The embedded view was blank in the initial Brave check; a separate-viewer link is available. These checks did not solve the security check, make a new payment, exercise disputes/tracking, or deploy the app.
