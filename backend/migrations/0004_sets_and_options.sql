-- Photo groups (a Telegram album, or a single photo), result sets, and per-job options.
-- Additive only: existing single-photo jobs keep working with all new columns null/false.

create table if not exists photo_groups (
  id           uuid primary key default gen_random_uuid(),
  customer_id  uuid not null references customers(id) on delete cascade,
  external_ref text not null,          -- e.g. Telegram media_group_id, or 'single:<upload id>'
  created_at   timestamptz not null default now(),
  unique (customer_id, external_ref)
);

alter table uploads add column if not exists group_id uuid references photo_groups(id) on delete set null;
create index if not exists uploads_group_idx on uploads (group_id);

create table if not exists generation_sets (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references customers(id) on delete cascade,
  group_id        uuid references photo_groups(id) on delete set null,
  channel         channel_t not null,
  mode            text not null check (mode in ('enhance', 'angles')),
  style           text not null,
  hide_plate      boolean not null default false,
  reply_ref       text,
  status          text not null default 'processing' check (status in ('processing', 'completed', 'partial', 'failed')),
  total_jobs      int not null default 0,
  idempotency_key text not null unique,
  created_at      timestamptz not null default now(),
  finished_at     timestamptz
);
create index if not exists generation_sets_customer_idx on generation_sets (customer_id, created_at);

alter table generation_jobs add column if not exists set_id uuid references generation_sets(id) on delete cascade;
alter table generation_jobs add column if not exists target_angle text;
alter table generation_jobs add column if not exists hide_plate boolean not null default false;
alter table generation_jobs add column if not exists reference_upload_ids uuid[];
create index if not exists generation_jobs_set_idx on generation_jobs (set_id);

alter table photo_groups enable row level security;
alter table generation_sets enable row level security;
revoke all on photo_groups from anon, authenticated;
revoke all on generation_sets from anon, authenticated;
