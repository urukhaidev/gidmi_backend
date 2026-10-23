-- Transactional helpers used by import_tripster_experiences.py.
-- Install once in the target Supabase project. Only service_role may call them.

create or replace function public.upsert_tripster_tag_bridge_v2(p_rows jsonb)
returns void
language plpgsql security invoker set search_path = '' as $function$
declare
  payload jsonb;
  previous_id integer;
  tag public.tags%rowtype;
begin
  for payload in select value from jsonb_array_elements(p_rows) loop
    select * into tag from public.tags
    where city_id = (payload->>'city_id')::integer and id = (payload->>'id')::integer
    for update;
    if found then
      previous_id := tag.citytag_id;
      tag := jsonb_populate_record(tag, payload);
      -- The legacy (city_id,id) index is partial. Resolve by that natural key,
      -- then migrate synthetic IDs; existing links follow ON UPDATE CASCADE.
      update public.tags set
        citytag_id=tag.citytag_id, name=tag.name, slug=tag.slug, category=tag.category,
        url=tag.url, is_hidden=tag.is_hidden, experience_count=tag.experience_count,
        excursions_count=tag.excursions_count, tours_count=tag.tours_count,
        review_count=tag.review_count, image_thumbnail=tag.image_thumbnail,
        image_medium=tag.image_medium, title=tag.title, header=tag.header,
        is_common=tag.is_common, is_auto=tag.is_auto,
        is_seo_only=tag.is_seo_only, is_attraction=tag.is_attraction
      where citytag_id=previous_id;
    else
      tag := jsonb_populate_record(null::public.tags, payload);
      tag.is_common := coalesce(tag.is_common, false);
      tag.is_auto := coalesce(tag.is_auto, false);
      tag.is_seo_only := coalesce(tag.is_seo_only, false);
      tag.is_attraction := coalesce(tag.is_attraction, false);
      insert into public.tags select tag.*;
    end if;
  end loop;
end;
$function$;

create or replace function public.replace_tripster_relations_v2(
  p_experience_ids integer[], p_tag_links jsonb, p_photos jsonb
) returns void
language plpgsql security invoker set search_path = '' as $function$
begin
  -- Serialize overlapping excursion snapshots while allowing different cities
  -- to commit in parallel. Lock order also prevents cyclic row-lock waits.
  perform 1 from public.experiences
  where id = any(p_experience_ids) order by id for update;
  if exists (
    select 1 from jsonb_to_recordset(p_tag_links) as r(experience_id integer)
    where r.experience_id is null or not (r.experience_id = any(p_experience_ids))
  ) or exists (
    select 1 from jsonb_to_recordset(p_photos) as r(experience_id integer)
    where r.experience_id is null or not (r.experience_id = any(p_experience_ids))
  ) then
    raise exception 'Relations outside the supplied experience scope';
  end if;

  delete from public.experience_tags where experience_id = any(p_experience_ids);
  insert into public.experience_tags (experience_id, citytag_id)
  select distinct r.experience_id, r.citytag_id
  from jsonb_to_recordset(p_tag_links) as r(experience_id integer, citytag_id integer);

  delete from public.experience_photos where experience_id = any(p_experience_ids);
  insert into public.experience_photos (experience_id, position, type, medium_url, thumbnail_url)
  select r.experience_id, r.position, r.type, r.medium_url, r.thumbnail_url
  from jsonb_to_recordset(p_photos) as r(
    experience_id integer, position integer, type text, medium_url text, thumbnail_url text
  );
end;
$function$;

create or replace function public.sync_experience_catalog_links_v2(p_experience_ids integer[])
returns integer
language plpgsql security invoker set search_path = '' as $function$
declare
  inserted_count integer;
begin
  perform 1 from public.experiences
  where id = any(p_experience_ids) order by id for update;
  -- Rebuild the desired set, including saved classifier decisions. The existing
  -- table has no source column, so classifier links are recovered from its source.
  delete from public.experience_tags_new where experience_id = any(p_experience_ids);
  with scoped_experiences as materialized (
    select id, lower(concat_ws(' ', title, annotation)) as searchable_text
    from public.experiences where id = any(p_experience_ids)
  ), direct_links as (
    select distinct c.id as catalog_id, et.experience_id
    from public.experience_tags et
    join public.tripster_city_tags ct on ct.citytag_id = et.citytag_id
    join public.tag_category_catalog c on c.tripster_tag_id = ct.tag_id
    where et.experience_id = any(p_experience_ids)
    union
    select distinct l.catalog_id, et.experience_id
    from public.experience_tags et
    join public.tripster_city_tags ct on ct.citytag_id = et.citytag_id
    join public.tag_category_links l on l.tag_id = ct.tag_id
    where et.experience_id = any(p_experience_ids)
  ), lexical_links as (
    select c.id as catalog_id, e.id as experience_id
    from public.tag_category_catalog c
    cross join scoped_experiences e
    where c.tripster_tag_id is null and c.is_common = true
      and c.is_hidden = false and cardinality(c.lexical_triggers) > 0
      and exists (
        select 1 from unnest(c.lexical_triggers) as t(value)
        where btrim(t.value) <> ''
          and position(lower(btrim(t.value)) in e.searchable_text) > 0
      )
  ), classified_links as (
    select c.id as catalog_id, t.experience_id
    from public.tag_experience_table t
    cross join lateral unnest(string_to_array(t.tags, ',')) as s(slug)
    join lateral (
      select id from public.tag_category_catalog
      where sub_slug = btrim(s.slug) and is_common = true
      order by id limit 1
    ) c on true
    where t.experience_id = any(p_experience_ids)
  )
  insert into public.experience_tags_new (catalog_id, experience_id)
  select catalog_id, experience_id from direct_links
  union select catalog_id, experience_id from lexical_links
  union select catalog_id, experience_id from classified_links;
  get diagnostics inserted_count = row_count;
  return inserted_count;
end;
$function$;

create or replace function public.finalize_tripster_catalog_refresh_v2(
  p_city_ids integer[], p_seen_ids integer[], p_started_at timestamptz
) returns integer
language plpgsql security invoker set search_path = '' as $function$
declare
  missing_ids integer[];
begin
  if p_started_at is null or p_city_ids is null or p_seen_ids is null then
    raise exception 'A complete refresh scope and start time are required';
  end if;
  -- last_synced_at protects rows refreshed by another concurrent import.
  select coalesce(array_agg(id), '{}'::integer[]) into missing_ids
  from (
    select e.id from public.experiences e
    where e.city_id = any(p_city_ids)
      and e.type in ('private', 'group', 'ticket', 'transfer_service')
      and not exists (select 1 from unnest(p_seen_ids) as seen(id) where seen.id = e.id)
      and (e.last_synced_at is null or e.last_synced_at < p_started_at)
    order by e.id limit 100
    for update of e
  ) locked;

  update public.experiences
  set status = 'inactive', last_synced_at = p_started_at
  where id = any(missing_ids);
  delete from public.experience_tags_new where experience_id = any(missing_ids);
  delete from public.experience_tags where experience_id = any(missing_ids);
  return cardinality(missing_ids);
end;
$function$;

revoke execute on function public.replace_tripster_relations_v2(integer[], jsonb, jsonb) from public, anon, authenticated;
revoke execute on function public.upsert_tripster_tag_bridge_v2(jsonb) from public, anon, authenticated;
revoke execute on function public.sync_experience_catalog_links_v2(integer[]) from public, anon, authenticated;
revoke execute on function public.finalize_tripster_catalog_refresh_v2(integer[], integer[], timestamptz) from public, anon, authenticated;
grant execute on function public.replace_tripster_relations_v2(integer[], jsonb, jsonb) to service_role;
grant execute on function public.upsert_tripster_tag_bridge_v2(jsonb) to service_role;
grant execute on function public.sync_experience_catalog_links_v2(integer[]) to service_role;
grant execute on function public.finalize_tripster_catalog_refresh_v2(integer[], integer[], timestamptz) to service_role;
