CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  snippet TEXT NOT NULL DEFAULT '',
  source_platform TEXT NOT NULL DEFAULT 'Web',
  source_domain TEXT NOT NULL DEFAULT '',
  search_query TEXT NOT NULL DEFAULT '',
  intent_type TEXT NOT NULL DEFAULT 'irrelevant',
  intent_score INTEGER NOT NULL DEFAULT 0,
  purchase_signal TEXT NOT NULL DEFAULT 'unknown',
  reason TEXT NOT NULL DEFAULT '',
  evidence_quote TEXT NOT NULL DEFAULT '',
  reply_draft TEXT NOT NULL DEFAULT '',
  need_summary TEXT NOT NULL DEFAULT '',
  buying_signal_summary TEXT NOT NULL DEFAULT '',
  need_clarity_score INTEGER NOT NULL DEFAULT 0,
  purchase_stage_score INTEGER NOT NULL DEFAULT 0,
  urgency_score INTEGER NOT NULL DEFAULT 0,
  service_fit_score INTEGER NOT NULL DEFAULT 0,
  contactability_score INTEGER NOT NULL DEFAULT 0,
  score_explanation TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  search_count INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(intent_score DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_platform ON leads(source_platform, intent_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);

CREATE TABLE IF NOT EXISTS search_runs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'queued',
  provider TEXT NOT NULL DEFAULT 'Serper',
  queries_total INTEGER NOT NULL DEFAULT 0,
  queries_completed INTEGER NOT NULL DEFAULT 0,
  raw_result_count INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  direct_requests INTEGER NOT NULL DEFAULT 0,
  high_intent_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  platform_counts_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  error_summary TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_search_runs_started ON search_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS consents (
  id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL,
  email TEXT NOT NULL,
  email_hash TEXT NOT NULL,
  source TEXT NOT NULL,
  referral_code TEXT NOT NULL DEFAULT '',
  preferred_style TEXT NOT NULL DEFAULT '',
  consent_text TEXT NOT NULL,
  verify_token_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  verified_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consents_email ON consents(email_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consents_source ON consents(source, verified_at);

CREATE TABLE IF NOT EXISTS email_followups (
  lead_id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  consent_confirmed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'consented',
  step INTEGER NOT NULL DEFAULT 0,
  next_send_at TEXT NOT NULL DEFAULT '',
  last_sent_at TEXT NOT NULL DEFAULT '',
  last_reply_at TEXT NOT NULL DEFAULT '',
  last_reply_subject TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_due ON email_followups(status, next_send_at);

CREATE TABLE IF NOT EXISTS email_events (
  id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL,
  email TEXT NOT NULL,
  step INTEGER NOT NULL,
  subject TEXT NOT NULL,
  status TEXT NOT NULL,
  provider_message_id TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_events_created ON email_events(created_at DESC);

CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL DEFAULT '',
  email_hash TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'direct',
  referral_code TEXT NOT NULL DEFAULT '',
  product TEXT NOT NULL,
  amount REAL NOT NULL,
  currency TEXT NOT NULL,
  ordered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_source ON orders(source, currency);
