-- Catalog metadata helper plus the legacy additive compatibility wrapper.
-- Run this file in Supabase SQL Editor to create or update the function.

create or replace function public.sync_tripster_local_catalog_v2()
returns void
language plpgsql
security invoker
set search_path = ''
as $function$
begin
  -- A tag can disappear from every refreshed city. Keep the catalog row for
  -- stable URLs, but hide it when it no longer has an eligible Tripster source.
  update public.tag_category_catalog c
  set is_hidden = true, city_ids = '{}'::integer[]
  where c.source = 'sync_tripster_local_categories_v1'
    and not exists (
      select 1 from public.tripster_tags tt
      join public.tripster_city_tags ct on ct.tag_id = tt.id
      where tt.id = c.tripster_tag_id
        and tt.is_common = false and tt.is_auto = false and tt.is_seo_only = false
        and coalesce(ct.experience_count, 0) > 0
    );

  with source_tags as (
    select
      tt.id as tag_id,
      array_agg(distinct ct.city_id order by ct.city_id) as city_ids,
      bool_and(coalesce(ct.is_hidden, false)) as is_hidden,
      (array_agg(ct.title order by coalesce(ct.is_hidden, false), ct.experience_count desc nulls last)
        filter (where nullif(ct.title, '') is not null))[1] as title,
      (array_agg(ct.header order by coalesce(ct.is_hidden, false), ct.experience_count desc nulls last)
        filter (where nullif(ct.header, '') is not null))[1] as header
    from public.tripster_tags tt
    join public.tripster_city_tags ct on ct.tag_id = tt.id
    where tt.is_common = false
      and tt.is_auto = false
      and tt.is_seo_only = false
      and coalesce(ct.experience_count, 0) > 0
    group by tt.id
  )
  update public.tag_category_catalog c
  set tripster_tag_id = s.tag_id,
      city_ids = s.city_ids,
      is_hidden = s.is_hidden,
      title = coalesce(s.title, c.title),
      header = coalesce(s.header, c.header)
  from source_tags s
  join public.tag_category_links l on l.tag_id = s.tag_id
  where l.catalog_id = c.id
    and c.source = 'sync_tripster_local_categories_v1';

  with source_tags as (
    select
      tt.id as tag_id,
      tt.name,
      coalesce(nullif(tt.slug, ''), 'tripster-tag-' || tt.id::text) as slug,
      tt.category as tripster_category,
      tt.is_attraction,
      array_agg(distinct ct.city_id order by ct.city_id) as city_ids,
      bool_and(coalesce(ct.is_hidden, false)) as is_hidden,
      (array_agg(ct.title order by coalesce(ct.is_hidden, false), ct.experience_count desc nulls last)
        filter (where nullif(ct.title, '') is not null))[1] as title,
      (array_agg(ct.header order by coalesce(ct.is_hidden, false), ct.experience_count desc nulls last)
        filter (where nullif(ct.header, '') is not null))[1] as header
    from public.tripster_tags tt
    join public.tripster_city_tags ct on ct.tag_id = tt.id
    where tt.is_common = false
      and tt.is_auto = false
      and tt.is_seo_only = false
      and coalesce(ct.experience_count, 0) > 0
    group by tt.id, tt.name, tt.slug, tt.category, tt.is_attraction
  ),
  unlinked as (
    select
      s.*,
      case
        when s.tripster_category in ('Достопримечательности', 'ТОП') or s.is_attraction
          then 'popularnye-mesta'
        when s.tripster_category = 'Форматы'
          then 'format'
        else 'tematicheskie'
      end as main_slug,
      case
        when s.tripster_category in ('Достопримечательности', 'ТОП') or s.is_attraction
          then 'Популярные места'
        when s.tripster_category = 'Форматы'
          then 'Формат'
        else 'Тематические'
      end as main_name
    from source_tags s
    where not exists (
      select 1 from public.tag_category_links l where l.tag_id = s.tag_id
    )
  ),
  upserted as (
    insert into public.tag_category_catalog (
      main_slug, main_name, sub_slug, sub_name,
      source, tripster_tag_id, is_common, is_hidden, city_ids, title, header
    )
    select
      main_slug, main_name, slug, name,
      'sync_tripster_local_categories_v1', tag_id, false, is_hidden, city_ids, title, header
    from unlinked
    on conflict (main_slug, sub_slug) do update
      set tripster_tag_id = case
            when public.tag_category_catalog.source = 'sync_tripster_local_categories_v1'
              then excluded.tripster_tag_id
            else public.tag_category_catalog.tripster_tag_id
          end,
          title = coalesce(public.tag_category_catalog.title, excluded.title),
          header = coalesce(public.tag_category_catalog.header, excluded.header)
    returning id, main_slug, sub_slug
  )
  insert into public.tag_category_links (catalog_id, tag_id)
  select u.id, s.tag_id
  from upserted u
  join unlinked s
    on s.main_slug = u.main_slug
   and s.slug = u.sub_slug
  on conflict do nothing;

end;
$function$;

create or replace function public.sync_tripster_local_categories_v1()
returns void
language plpgsql
security invoker
set search_path = ''
as $function$
begin
  perform public.sync_tripster_local_catalog_v2();

  insert into public.experience_tags_new (catalog_id, experience_id)
  select distinct l.catalog_id, et.experience_id
  from public.tripster_tags tt
  join public.tripster_city_tags ct on ct.tag_id = tt.id
  join public.experience_tags et on et.citytag_id = ct.citytag_id
  join public.tag_category_links l on l.tag_id = tt.id
  where tt.is_common = false
    and tt.is_auto = false
    and tt.is_seo_only = false
    and coalesce(ct.experience_count, 0) > 0
  on conflict do nothing;
end;
$function$;

revoke execute on function public.sync_tripster_local_categories_v1() from public, anon, authenticated;
revoke execute on function public.sync_tripster_local_catalog_v2() from public, anon, authenticated;
grant execute on function public.sync_tripster_local_catalog_v2() to service_role;
