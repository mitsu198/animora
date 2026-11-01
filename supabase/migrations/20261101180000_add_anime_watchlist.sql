-- ANIMORA personal anime lists and status tracking
create table if not exists public.animora_watchlist (
  user_id uuid not null references auth.users(id) on delete cascade,
  anime_id text not null,
  title text not null,
  poster text not null default '',
  genre text not null default '',
  status text not null check (status in ('watching','planned','watched','dropped','favorite')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, anime_id)
);
alter table public.animora_watchlist enable row level security;
drop policy if exists "Users manage own animora watchlist" on public.animora_watchlist;
create policy "Users manage own animora watchlist" on public.animora_watchlist for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create or replace function public.animora_watchlist_touch_updated_at() returns trigger language plpgsql as $$ begin new.updated_at = now(); return new; end; $$;
drop trigger if exists animora_watchlist_updated_at on public.animora_watchlist;
create trigger animora_watchlist_updated_at before update on public.animora_watchlist for each row execute function public.animora_watchlist_touch_updated_at();
grant select, insert, update, delete on public.animora_watchlist to authenticated;
