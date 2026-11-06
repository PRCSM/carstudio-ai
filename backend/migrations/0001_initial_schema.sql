-- CarStudio AI — initial schema
-- All access goes through FastAPI (service connection). RLS is enabled on every
-- table with no policies, so the public anon/authenticated keys can read nothing.

create extension if not exists pgcrypto;

-- ---------- enums ----------
create type channel_t as enum ('whatsapp', 'web');
create type job_status_t as enum (
  'queued', 'validating', 'analyzing', 'preparing', 'generating', 'verifying',
  'completed', 'failed', 'cancelled', 'review_required', 'needs_more_images'
);
create type conversation_state_t as enum (
  'new', 'awaiting_image', 'awaiting_style', 'confirmed', 'processing',
  'completed', 'needs_more_images', 'review_required', 'failed'
);
create type qa_status_t as enum ('pending', 'approved', 'retry', 'review_required', 'rejected');
create type delivery_status_t as enum ('pending', 'sent', 'delivered', 'failed');
create type review_decision_t as enum ('approve', 'reject', 'request_more_images');

-- ---------- customers & conversations ----------
create table customers (
  id               uuid primary key default gen_random_uuid(),
  channel          channel_t not null,
  external_ref_hash text not null,          -- sha256(phone) or web session id hash; never raw phone
  created_at       timestamptz not null default now(),
  consent_version  text,
  consent_at       timestamptz,
  retention_days   int not null default 30,
  unique (channel, external_ref_hash)
);

create table conversations (
  id             uuid primary key default gen_random_uuid(),
  customer_id    uuid not null references customers(id) on delete cascade,
  state          conversation_state_t not null default 'new',
  selected_style text,
  pending_image_id uuid,
  updated_at     timestamptz not null default now(),
  expires_at     timestamptz not null default now() + interval '24 hours',
  unique (customer_id)
);

create table inbound_messages (
  id                  uuid primary key default gen_random_uuid(),
  customer_id         uuid references customers(id) on delete cascade,
  provider_message_id text not null unique,
  media_id            text,
  received_at         timestamptz not null default now(),
  processed_at        timestamptz
);

-- ---------- vehicles & images ----------
create table vehicles (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references customers(id) on delete cascade,
  profile         jsonb,
  profile_version text,
  created_at      timestamptz not null default now()
);

create table uploads (
  id           uuid primary key default gen_random_uuid(),
  customer_id  uuid not null references customers(id) on delete cascade,
  storage_key  text not null,
  sha256       text not null,
  mime_type    text not null,
  width        int not null,
  height       int not null,
  byte_size    int not null,
  quality      jsonb,                        -- blur/exposure heuristics
  created_at   timestamptz not null default now()
);

create table vehicle_images (
  id           uuid primary key default gen_random_uuid(),
  vehicle_id   uuid not null references vehicles(id) on delete cascade,
  upload_id    uuid not null references uploads(id) on delete cascade,
  source_angle text,
  created_at   timestamptz not null default now()
);

-- ---------- jobs ----------
create table generation_jobs (
  id               uuid primary key default gen_random_uuid(),
  customer_id      uuid not null references customers(id) on delete cascade,
  vehicle_id       uuid references vehicles(id) on delete set null,
  source_upload_id uuid not null references uploads(id) on delete cascade,
  channel          channel_t not null,
  style            text not null,
  mode             text not null default 'same_angle',
  status           job_status_t not null default 'queued',
  attempt_count    int not null default 0,
  error_code       text,
  error_message    text,
  idempotency_key  text not null unique,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz
);

create table job_events (
  id          bigint generated always as identity primary key,
  job_id      uuid not null references generation_jobs(id) on delete cascade,
  from_status job_status_t,
  to_status   job_status_t not null,
  reason      text,
  created_at  timestamptz not null default now()
);

create table generated_images (
  id             uuid primary key default gen_random_uuid(),
  job_id         uuid not null references generation_jobs(id) on delete cascade,
  attempt        int not null,
  storage_key    text,
  angle          text,
  provider       text not null,
  model_id       text,
  prompt_version text,
  qa_status      qa_status_t not null default 'pending',
  qa             jsonb,
  created_at     timestamptz not null default now()
);

-- ---------- delivery, outbox, review, usage ----------
create table delivery_events (
  id                  uuid primary key default gen_random_uuid(),
  job_id              uuid not null references generation_jobs(id) on delete cascade,
  channel             channel_t not null,
  status              delivery_status_t not null default 'pending',
  provider_message_id text,
  attempt_count       int not null default 0,
  last_error          text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create table outbox_events (
  id            uuid primary key default gen_random_uuid(),
  event_type    text not null,
  payload       jsonb not null,
  published_at  timestamptz,
  attempt_count int not null default 0,
  next_attempt_at timestamptz not null default now(),
  last_error    text,
  created_at    timestamptz not null default now()
);

create table review_actions (
  id          uuid primary key default gen_random_uuid(),
  job_id      uuid not null references generation_jobs(id) on delete cascade,
  reviewer_id text not null,
  decision    review_decision_t not null,
  reason      text,
  created_at  timestamptz not null default now()
);

create table usage_events (
  id             uuid primary key default gen_random_uuid(),
  customer_id    uuid references customers(id) on delete set null,
  job_id         uuid references generation_jobs(id) on delete set null,
  provider       text not null,
  kind           text not null,               -- 'generation' | 'vision' | 'qa'
  units          numeric not null default 1,
  estimated_cost_usd numeric(10,5) not null default 0,
  created_at     timestamptz not null default now()
);

-- ---------- indexes ----------
create index on generation_jobs (status, created_at);
create index on generation_jobs (customer_id, created_at);
create index on generated_images (job_id);
create index on job_events (job_id);
create index on outbox_events (next_attempt_at) where published_at is null;
create index on usage_events (created_at);
create index on usage_events (customer_id, created_at);
create index on uploads (customer_id, created_at);
create index on delivery_events (job_id);

-- ---------- lock down PostgREST access ----------
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
  end loop;
end $$;
