-- ANIMORA: безопасный учёт времени просмотра для авторизованных пользователей.
-- Применить один раз в Supabase SQL Editor перед включением функции в приложении.

create table if not exists public.animora_watch_time (
    user_id uuid primary key references auth.users(id) on delete cascade,
    display_name text not null default 'Пользователь',
    total_seconds bigint not null default 0 check (total_seconds >= 0),
    updated_at timestamptz not null default now()
);

alter table public.animora_watch_time enable row level security;

revoke all on table public.animora_watch_time from anon, authenticated;
grant select on table public.animora_watch_time to authenticated;

drop policy if exists "Users can read own watch time" on public.animora_watch_time;
create policy "Users can read own watch time"
    on public.animora_watch_time
    for select
    to authenticated
    using (auth.uid() = user_id);

create or replace function public.increment_watch_time(
    p_seconds integer,
    p_display_name text default 'Пользователь'
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_user_id uuid := auth.uid();
    v_seconds integer := greatest(0, least(coalesce(p_seconds, 0), 30));
    v_display_name text := left(coalesce(nullif(trim(p_display_name), ''), 'Пользователь'), 80);
begin
    if v_user_id is null then
        raise exception 'Authentication required' using errcode = '28000';
    end if;

    if v_seconds < 1 then
        return;
    end if;

    insert into public.animora_watch_time (user_id, display_name, total_seconds, updated_at)
    values (v_user_id, v_display_name, v_seconds, now())
    on conflict (user_id) do update
    set display_name = excluded.display_name,
        total_seconds = public.animora_watch_time.total_seconds + excluded.total_seconds,
        updated_at = now();
end;
$$;

revoke all on function public.increment_watch_time(integer, text) from public, anon;
grant execute on function public.increment_watch_time(integer, text) to authenticated;
