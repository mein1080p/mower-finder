-- ============================================================================
-- Mower Finder database schema
-- Run this ONCE in your Supabase SQL Editor after connecting the app.
-- If you're adding to an existing project, the `mower_` prefix keeps these
-- tables from colliding with anything else you have.
-- ============================================================================

-- Listings table: one row per unique mower listing found
create table if not exists mower_listings (
    id              bigserial primary key,
    fingerprint     text unique not null,
    source          text not null,
    title           text not null,
    url             text not null,
    snippet         text,
    price           text,
    location        text,
    brand           text default 'unknown',
    quantity        integer default 1,
    is_bulk         boolean default false,
    first_seen      timestamptz not null default now(),
    last_seen       timestamptz not null default now(),
    query           text,
    status          text not null default 'new',
    notes           text
);

create index if not exists idx_mower_listings_bulk
    on mower_listings(is_bulk, status);
create index if not exists idx_mower_listings_first_seen
    on mower_listings(first_seen desc);
create index if not exists idx_mower_listings_brand
    on mower_listings(brand);

-- Settings table: key-value store for editable config (model lists, queries, etc.)
create table if not exists mower_settings (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null default now()
);

-- Note on security:
-- The app connects using the service_role key (stored in Streamlit secrets),
-- which bypasses row-level security. Since this is your private sourcing tool
-- and the key is only in your Streamlit Cloud secrets, that's fine.
-- If you later want public-facing features, enable RLS and add policies.
