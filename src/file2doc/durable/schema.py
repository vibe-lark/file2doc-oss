SCHEMA_VERSION = 2

SCHEMA_SQL = """
create table if not exists file2doc_schema_migrations (
    version integer primary key,
    applied_at timestamptz not null default now()
);

create table if not exists parse_jobs (
    job_id text primary key,
    status text not null,
    stage text not null,
    percent integer not null check (percent between 0 and 100),
    document jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    expires_at timestamptz not null
);

create index if not exists parse_jobs_status_created
    on parse_jobs (status, created_at);

create table if not exists progress_events (
    event_id text primary key,
    job_id text not null references parse_jobs(job_id) on delete cascade,
    sequence integer not null,
    document jsonb not null,
    created_at timestamptz not null,
    unique (job_id, sequence)
);

create table if not exists work_items (
    work_item_id text primary key,
    job_id text not null references parse_jobs(job_id) on delete cascade,
    kind text not null,
    status text not null check (
        status in ('pending', 'leased', 'completed', 'failed', 'cancelled')
    ),
    payload jsonb not null default '{}'::jsonb,
    result jsonb,
    depends_on text[] not null default '{}',
    attempt_count integer not null default 0,
    max_attempts integer not null default 3 check (max_attempts > 0),
    available_at timestamptz not null,
    lease_owner text,
    lease_token text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    last_error jsonb,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    unique (job_id, kind)
);

alter table work_items add column if not exists lease_token text;

create index if not exists work_items_claimable
    on work_items (kind, available_at, created_at)
    where status in ('pending', 'leased');

create index if not exists work_items_job
    on work_items (job_id, created_at);

create table if not exists execution_attempts (
    work_item_id text not null references work_items(work_item_id) on delete cascade,
    attempt_number integer not null check (attempt_number > 0),
    lease_token text not null unique,
    worker_id text not null,
    status text not null check (
        status in ('active', 'completed', 'failed', 'expired')
    ),
    started_at timestamptz not null,
    heartbeat_at timestamptz not null,
    lease_expires_at timestamptz not null,
    finished_at timestamptz,
    error jsonb,
    primary key (work_item_id, attempt_number)
);

create index if not exists execution_attempts_status_lease
    on execution_attempts (status, lease_expires_at);

create table if not exists artifact_publications (
    job_id text not null references parse_jobs(job_id) on delete cascade,
    artifact_id text not null,
    work_item_id text references work_items(work_item_id) on delete set null,
    state text not null check (state in ('staging', 'published')),
    object_key text not null,
    media_type text not null,
    sha256 text not null,
    size_bytes bigint not null,
    document jsonb not null,
    created_at timestamptz not null,
    published_at timestamptz,
    primary key (job_id, artifact_id),
    unique (object_key)
);
"""
