CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  price NUMERIC(12,2) NOT NULL, cost NUMERIC(12,2) NOT NULL DEFAULT 0,
  attributes JSONB NOT NULL DEFAULT '{}', image_url TEXT);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, claimed_platform TEXT NOT NULL, public_key TEXT,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS shopping_candidates (
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, snapshot JSONB NOT NULL,
  browser JSONB, observation JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('PENDING_CONFIRMATION','CONFIRMED','ORDERED','CAPTURED','CANCELLED','EXPIRED')),
  instruction TEXT NOT NULL, constraints JSONB NOT NULL, cart JSONB NOT NULL,
  agent_id TEXT REFERENCES agents(id), agent_sig TEXT, agent_sig_ok BOOLEAN,
  confirmation_method TEXT, seal_hash CHAR(64), merchant_sig TEXT, key_id TEXT NOT NULL DEFAULT 'k1',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL);

CREATE TABLE IF NOT EXISTS receipt_events (
  receipt_id TEXT NOT NULL REFERENCES receipts(id), seq INT NOT NULL, type TEXT NOT NULL,
  payload JSONB NOT NULL, at TEXT NOT NULL, prev_hash CHAR(64) NOT NULL, hash CHAR(64) NOT NULL,
  PRIMARY KEY (receipt_id, seq));

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'receipt_events is append-only'; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS receipt_events_immutable ON receipt_events;
CREATE TRIGGER receipt_events_immutable BEFORE UPDATE OR DELETE ON receipt_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE IF NOT EXISTS orders (
  paypal_order_id TEXT PRIMARY KEY, receipt_id TEXT REFERENCES receipts(id), capture_id TEXT UNIQUE,
  status TEXT NOT NULL, amount NUMERIC(12,2) NOT NULL, currency CHAR(3) NOT NULL DEFAULT 'USD',
  payer_id TEXT, shipping_address JSONB, custom_id TEXT, request_id UUID NOT NULL, raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS shipments (
  id BIGSERIAL PRIMARY KEY, paypal_order_id TEXT NOT NULL REFERENCES orders(paypal_order_id),
  tracker_id TEXT, carrier TEXT NOT NULL, tracking_number TEXT NOT NULL, status TEXT NOT NULL,
  delivered_at TIMESTAMPTZ, raw JSONB);

CREATE TABLE IF NOT EXISTS disputes (
  dispute_id TEXT PRIMARY KEY, capture_id TEXT, paypal_order_id TEXT, reason TEXT, status TEXT, stage TEXT,
  amount NUMERIC(12,2), currency CHAR(3), created_at_pp TIMESTAMPTZ, due_at TIMESTAMPTZ,
  raw JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS cases (
  dispute_id TEXT PRIMARY KEY REFERENCES disputes(dispute_id),
  state TEXT NOT NULL CHECK (state IN ('NEW','ANALYZING','AWAITING_APPROVAL','SUBMITTED','WON','LOST','ACCEPTED','EXPIRED','NEEDS_ATTENTION','MANUAL')),
  thread_id TEXT, triage JSONB, economics JSONB, override JSONB, receipt_strength TEXT,
  evidence JSONB, evidence_gaps JSONB, draft JSONB, check_issues JSONB,
  pdf BYTEA, pdf_sha256 CHAR(64), payload JSONB, payload_hash CHAR(64), pending_action JSONB,
  error TEXT, outcome TEXT, dry_run BOOLEAN NOT NULL DEFAULT false, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS issues (
  id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL REFERENCES receipts(id), capture_id TEXT,
  type TEXT NOT NULL, description TEXT NOT NULL, desired_resolution TEXT NOT NULL,
  proposal JSONB, state TEXT NOT NULL CHECK (state IN ('OPEN','PROPOSED','OFFERED','RESOLVED','REJECTED')),
  countered BOOLEAN NOT NULL DEFAULT false, resolution JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS approvals (
  id BIGSERIAL PRIMARY KEY, dispute_id TEXT REFERENCES disputes(dispute_id), issue_id TEXT REFERENCES issues(id),
  user_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('PROVIDE_EVIDENCE','MAKE_OFFER','ACCEPT_CLAIM','REFUND','REPLACEMENT')),
  payload_hash CHAR(64) NOT NULL, approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  consumed_at TIMESTAMPTZ, response JSONB, http_status INT);

CREATE TABLE IF NOT EXISTS webhook_events (
  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, verified BOOLEAN NOT NULL, resource_id TEXT,
  payload JSONB NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT now(), processed_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS history_txns (
  id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL, amount NUMERIC(12,2) NOT NULL,
  disputed BOOLEAN NOT NULL DEFAULT false, simulated BOOLEAN NOT NULL DEFAULT true);

CREATE INDEX IF NOT EXISTS disputes_due ON disputes (due_at);
