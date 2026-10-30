-- ANIMORA: watch-time leaderboard
create table if not exists public.watch_time (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default 'Зритель',
  seconds bigint not null default 0 check (seconds >= 0),
  updated_at timestamptz not null default now()
);

alter table public.watch_time enable row level security;

drop policy if exists "Leaderboard is publicly readable" on public.watch_time;
create policy "Leaderboard is publicly readable"
  on public.watch_time for select
  using (true);

grant select on public.watch_time to anon, authenticated;

create or replace function public.increment_watch_time(
  p_seconds integer,
  p_display_name text
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  safe_seconds integer;
  safe_name text;
begin
  if auth.uid() is null then
    raise exception 'Authentication required';
  end if;
  safe_seconds := greatest(0, least(coalesce(p_seconds, 0), 30));
  if safe_seconds < 1 then
    raise exception 'Invalid seconds';
  end if;
  safe_name := left(coalesce(nullif(trim(p_display_name), ''), 'Зритель'), 24);

  insert into public.watch_time(user_id, display_name, seconds, updated_at)
  values (auth.uid(), safe_name, safe_seconds, now())
  on conflict (user_id) do update
    set display_name = excluded.display_name,
        seconds = public.watch_time.seconds + excluded.seconds,
        updated_at = now();
end;
$$;

revoke all on function public.increment_watch_time(integer, text) from public;
grant execute on function public.increment_watch_time(integer, text) to authenticated;
