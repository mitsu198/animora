-- ANIMORA economy: run this migration in Supabase SQL Editor before enabling the new pages.
create table if not exists public.animora_wallets (
  user_id uuid primary key references auth.users(id) on delete cascade,
  balance bigint not null default 0 check (balance >= 0),
  xp bigint not null default 0 check (xp >= 0),
  level integer not null default 1 check (level >= 1),
  premium_until timestamptz,
  updated_at timestamptz not null default now()
);
create table if not exists public.animora_achievements (
  user_id uuid not null references auth.users(id) on delete cascade,
  achievement_key text not null,
  completed_at timestamptz not null default now(),
  primary key (user_id, achievement_key)
);
create table if not exists public.animora_inventory (
  user_id uuid not null references auth.users(id) on delete cascade,
  item_key text not null,
  purchased_at timestamptz not null default now(),
  primary key (user_id, item_key)
);
create table if not exists public.animora_profile_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  banner_key text,
  frame_key text,
  updated_at timestamptz not null default now()
);
alter table public.animora_wallets enable row level security;
alter table public.animora_achievements enable row level security;
alter table public.animora_inventory enable row level security;
alter table public.animora_profile_settings enable row level security;
drop policy if exists "wallet owner reads own wallet" on public.animora_wallets;
create policy "wallet owner reads own wallet" on public.animora_wallets for select to authenticated using (auth.uid() = user_id);
drop policy if exists "achievements owner reads own achievements" on public.animora_achievements;
create policy "achievements owner reads own achievements" on public.animora_achievements for select to authenticated using (auth.uid() = user_id);
drop policy if exists "inventory owner reads own inventory" on public.animora_inventory;
create policy "inventory owner reads own inventory" on public.animora_inventory for select to authenticated using (auth.uid() = user_id);
drop policy if exists "profile settings owner reads own settings" on public.animora_profile_settings;
create policy "profile settings owner reads own settings" on public.animora_profile_settings for select to authenticated using (auth.uid() = user_id);
grant select on public.animora_wallets, public.animora_achievements, public.animora_inventory, public.animora_profile_settings to authenticated;

create or replace function public.ensure_animora_wallet()
returns public.animora_wallets language plpgsql security definer set search_path=public as $$
declare result public.animora_wallets;
begin
  if auth.uid() is null then raise exception 'Authentication required'; end if;
  insert into public.animora_wallets(user_id) values(auth.uid()) on conflict(user_id) do nothing;
  select * into result from public.animora_wallets where user_id=auth.uid();
  return result;
end; $$;

create or replace function public.claim_animora_achievement(p_key text)
returns jsonb language plpgsql security definer set search_path=public as $$
declare uid uuid := auth.uid(); reward_coins integer; reward_xp integer; eligible boolean := false;
begin
  if uid is null then raise exception 'Authentication required'; end if;
  case p_key
    when 'first_favorite' then
      reward_coins := 100; reward_xp := 25;
      select exists(select 1 from public.favorites where user_id=uid) into eligible;
    when 'collector_5' then
      reward_coins := 250; reward_xp := 60;
      select count(*) >= 5 from public.favorites where user_id=uid into eligible;
    when 'watch_30m' then
      reward_coins := 200; reward_xp := 50;
      select coalesce(seconds,0) >= 1800 from public.watch_time where user_id=uid into eligible;
    when 'watch_2h' then
      reward_coins := 600; reward_xp := 150;
      select coalesce(seconds,0) >= 7200 from public.watch_time where user_id=uid into eligible;
    else raise exception 'Unknown achievement';
  end case;
  if not coalesce(eligible,false) then raise exception 'Achievement requirements not met'; end if;
  insert into public.animora_achievements(user_id,achievement_key) values(uid,p_key) on conflict do nothing;
  if not found then return jsonb_build_object('success',false,'message','Achievement already claimed'); end if;
  insert into public.animora_wallets(user_id) values(uid) on conflict(user_id) do nothing;
  update public.animora_wallets set balance=balance+reward_coins, xp=xp+reward_xp,
    level=1+floor((xp+reward_xp)/100.0)::integer, updated_at=now()
    where user_id=uid;
  return jsonb_build_object('success',true,'coins',reward_coins,'xp',reward_xp);
end; $$;

create or replace function public.buy_animora_item(p_item_key text)
returns jsonb language plpgsql security definer set search_path=public as $$
declare uid uuid := auth.uid(); cost integer; is_banner boolean := false; is_frame boolean := false;
begin
  if uid is null then raise exception 'Authentication required'; end if;
  case p_item_key
    when 'banner_sakura' then cost:=1500; is_banner:=true;
    when 'banner_neon' then cost:=2500; is_banner:=true;
    when 'banner_galaxy' then cost:=4000; is_banner:=true;
    when 'frame_violet' then cost:=1000; is_frame:=true;
    when 'frame_gold' then cost:=3000; is_frame:=true;
    else raise exception 'Unknown shop item';
  end case;
  insert into public.animora_wallets(user_id) values(uid) on conflict(user_id) do nothing;
  if exists(select 1 from public.animora_inventory where user_id=uid and item_key=p_item_key) then
    return jsonb_build_object('success',false,'message','Item already owned');
  end if;
  update public.animora_wallets set balance=balance-cost,updated_at=now()
    where user_id=uid and balance>=cost;
  if not found then raise exception 'Not enough AniCoins'; end if;
  insert into public.animora_inventory(user_id,item_key) values(uid,p_item_key);
  if is_banner then
    insert into public.animora_profile_settings(user_id,banner_key) values(uid,p_item_key)
    on conflict(user_id) do update set banner_key=excluded.banner_key,updated_at=now();
  elsif is_frame then
    insert into public.animora_profile_settings(user_id,frame_key) values(uid,p_item_key)
    on conflict(user_id) do update set frame_key=excluded.frame_key,updated_at=now();
  end if;
  return jsonb_build_object('success',true,'cost',cost);
end; $$;

create or replace function public.buy_animora_premium(p_days integer default 30)
returns jsonb language plpgsql security definer set search_path=public as $$
declare uid uuid := auth.uid(); cost integer; current_expiry timestamptz;
begin
  if uid is null then raise exception 'Authentication required'; end if;
  if p_days not in (7,30,90) then raise exception 'Invalid premium period'; end if;
  cost := case p_days when 7 then 5000 when 30 then 15000 when 90 then 40000 end;
  insert into public.animora_wallets(user_id) values(uid) on conflict(user_id) do nothing;
  update public.animora_wallets set balance=balance-cost,updated_at=now()
    where user_id=uid and balance>=cost;
  if not found then raise exception 'Not enough AniCoins'; end if;
  select premium_until into current_expiry from public.animora_wallets where user_id=uid;
  update public.animora_wallets set premium_until=greatest(coalesce(current_expiry,now()),now()) + make_interval(days=>p_days),
    updated_at=now() where user_id=uid;
  return jsonb_build_object('success',true,'days',p_days);
end; $$;

revoke all on function public.ensure_animora_wallet() from public;
revoke all on function public.claim_animora_achievement(text) from public;
revoke all on function public.buy_animora_item(text) from public;
revoke all on function public.buy_animora_premium(integer) from public;
grant execute on function public.ensure_animora_wallet() to authenticated;
grant execute on function public.claim_animora_achievement(text) to authenticated;
grant execute on function public.buy_animora_item(text) to authenticated;
grant execute on function public.buy_animora_premium(integer) to authenticated;

-- Reward a small amount of progression for verified watch time, server-side.
create or replace function public.increment_watch_time(p_seconds integer, p_display_name text)
returns void language plpgsql security definer set search_path=public as $$
declare safe_seconds integer; safe_name text;
begin
  if auth.uid() is null then raise exception 'Authentication required'; end if;
  safe_seconds := greatest(0, least(coalesce(p_seconds,0),30));
  if safe_seconds < 1 then raise exception 'Invalid seconds'; end if;
  safe_name := left(coalesce(nullif(trim(p_display_name),''),'Зритель'),24);
  insert into public.watch_time(user_id,display_name,seconds,updated_at)
  values(auth.uid(),safe_name,safe_seconds,now())
  on conflict(user_id) do update set display_name=excluded.display_name,
    seconds=public.watch_time.seconds+excluded.seconds,updated_at=now();
  insert into public.animora_wallets(user_id) values(auth.uid()) on conflict(user_id) do nothing;
  update public.animora_wallets set xp=xp+greatest(1,floor(safe_seconds/30.0)::integer),
    level=1+floor((xp+greatest(1,floor(safe_seconds/30.0)::integer))/100.0)::integer,updated_at=now()
    where user_id=auth.uid();
end; $$;
revoke all on function public.increment_watch_time(integer,text) from public;
grant execute on function public.increment_watch_time(integer,text) to authenticated;
