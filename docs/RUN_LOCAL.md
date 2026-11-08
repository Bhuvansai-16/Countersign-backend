# Run and test Countersign on this Windows machine

The US PayPal sandbox app is configured in the ignored backend `.env`. Use PowerShell. The existing Python
environment is outside OneDrive. Do not start another server on a port that is already serving Countersign.

## 1. Check whether the app is already running

```powershell
Invoke-RestMethod http://localhost:8010/healthz
```

Expected: `ok = True`, `db = True`. Open `http://localhost:3000` for the landing page, then **Open app** for shopping without login. `/login` is only for the merchant console.
If both work, skip startup and go to step 4.

## 2. Start the backend (PowerShell window 1)

```powershell
Set-Location 'C:\Users\Bhuvansai\OneDrive\Documents\Paypalhack\Countersign-backend'
$py = 'C:\Users\Bhuvansai\.venvs\countersign-backend\Scripts\python.exe'

# Recover the existing dev database and refresh its port after a restart.
$devDb = & $py -c 'import pathlib, pgserver; print(pgserver.get_server(pathlib.Path("C:/Users/Bhuvansai/.countersign-pg-dev"), cleanup_mode=None).get_uri())'
if ($LASTEXITCODE -ne 0) { throw 'Database is still recovering; retry the previous command before continuing.' }
& $py -m dotenv -f .env set DATABASE_URL $devDb

& $py -m uvicorn app.main:build --factory --env-file .env --loop asyncio:SelectorEventLoop --host 127.0.0.1 --port 8010
```

Leave this window open. Expected: `Application startup complete` and a listener on port 8010.
The embedded database may exceed its initial 10-second startup timeout during crash recovery; retry the database
command once recovery has finished. Do not delete or reinitialize its data directory.

## 3. Start the frontend (PowerShell window 2)

```powershell
Set-Location 'C:\Users\Bhuvansai\OneDrive\Documents\Paypalhack\Countersign'
$env:NEXT_PUBLIC_API_BASE = 'http://localhost:8010'
node node_modules/next/dist/bin/next dev --port 3000
```

Leave this window open. Use `http://localhost:3000` (the backend CORS configuration expects this origin).
The machine's roaming `npm` / `npx` shims are broken; the direct project executables above avoid them.

## 4. Inspect the completed buyer purchase

1. Open `http://localhost:3000/confirm/rcpt_68bf79004ca5cdd170749995`.
2. Review the sage throw: item $78 + shipping $6 = **$84**, under the **$90** cap.
3. This order is already paid: expect **Paid. Thank you!**, capture `3L906348KK120212Y`.
4. Open `http://localhost:8010/verify/rcpt_68bf79004ca5cdd170749995` and expect **STRONG** with matching integrity checks.

To repeat the flow, create a **fresh mandate** in step 5, open its link, click **Confirm this purchase**, then
**Pay with PayPal**. Sign in with Buyer A: `sb-v3mdl53715004@personal.example.com`.
   Find its generated test password yourself in PayPal Developer -> Testing Tools -> Sandbox Accounts ->
   that Personal account -> account details. Use the sandbox password, not your real PayPal password.
Approve the sandbox purchase. Let PayPal return to Countersign; the return page captures automatically and shows
**Paid. Thank you!**, a capture ID, and a verification link. Expect **STRONG** after successful approval/capture.

The completed US PayPal order is `2NG228202S383952F`. Do not reuse it for a second purchase.

Sandbox accounts:

| Role | Email |
|---|---|
| US seller (app owner) | sb-iaenv53656392@business.example.com |
| Buyer A | sb-v3mdl53715004@personal.example.com |
| Buyer B (reserve) | sb-pprgt53715166@personal.example.com |

## 5. Create a fresh checkout with the shopping agent (PowerShell window 3)

```powershell
Set-Location 'C:\Users\Bhuvansai\OneDrive\Documents\Paypalhack\Countersign-backend'
$py = 'C:\Users\Bhuvansai\.venvs\countersign-backend\Scripts\python.exe'
& $py -m dotenv -f .env run -- $py scripts/buyer_agent.py 'Buy one linen throw in sage under 90 USD'
```

This uses the configured LLM and MCP agent key. Open the new confirmation URL printed by the agent, then repeat
step 4. It consumes LLM credits. The catalog is already seeded; if restoring a new dev database, seed with:

```powershell
& $py -m dotenv -f .env run -- $py scripts/seed.py
```

## 6. Inspect the seller console

1. Open `http://localhost:3000/login`; enter `ADMIN_PASSWORD` from the backend `.env` locally.
2. Open **Orders** (`/orders`): the captured order should appear with its receipt strength.
3. Open **Disputes** (`/cases`): an empty queue is expected until a real sandbox dispute is opened.
4. Open **Agent issues** (`/issues`): empty is expected until an agent reports an issue.

After payment, the next unverified phase is the dispute lifecycle. In the sandbox buyer's PayPal Activity /
Resolution Center, select the paid transaction and report a problem if that action is available. Record the
**buyer-side transaction ID**; it may differ from the seller capture ID. The backend poller checks disputes every
five minutes. A created dispute should then appear in `/cases`; inspect its draft and PDF before approving.

The `/sim` page exists, but automatic dispute creation, evidence submission, escalation and adjudication are still
verification work. Simulator routes are disabled unless `SIMULATOR_ENABLED=true`; the demo button additionally
needs `DEMO_ENABLED=true` and a populated `DEMO_DISPUTE_POOL`. Do not treat these paths as proven yet.

## 7. Run checks

Backend (the suite uses a separate test DB and excludes live LLM evals):

```powershell
Set-Location 'C:\Users\Bhuvansai\OneDrive\Documents\Paypalhack\Countersign-backend'
& 'C:\Users\Bhuvansai\.venvs\countersign-backend\Scripts\python.exe' -m pytest -q
```

Frontend:

```powershell
Set-Location 'C:\Users\Bhuvansai\OneDrive\Documents\Paypalhack\Countersign'
node node_modules/typescript/bin/tsc --noEmit
node node_modules/eslint/bin/eslint.js .
```

Last verified Nov 5: **155 backend tests passed**, 11 live LLM tests deselected; frontend type-check and lint passed
before the backend-only capture fix (no frontend source changes).
Secrets remain in the ignored `.env`; never paste passwords, client secrets or bearer tokens into the repo or chat.
