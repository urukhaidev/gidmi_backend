-- Builds experience-to-category links from direct Tripster tags and lexical triggers.

create or replace function public.sync_experience_catalog_links_v1()
returns void
language plpgsql
security invoker
set search_path = ''
as $function$
begin
  -- Direct links for catalog categories that reference a Tripster tag.
  insert into public.experience_tags_new (catalog_id, experience_id)
  select distinct c.id, et.experience_id
  from public.tag_category_catalog c
  join public.tripster_city_tags ct on ct.tag_id = c.tripster_tag_id
  join public.experience_tags et on et.citytag_id = ct.citytag_id
  where c.tripster_tag_id is not null
  on conflict do nothing;

-- Common categories without a Tripster tag: match their lexical triggers
-- against an excursion's title and annotation.
  with common_categories as materialized (
    select id, lexical_triggers
    from public.tag_category_catalog
    where tripster_tag_id is null
      and is_common = true
      and is_hidden = false
      and cardinality(lexical_triggers) > 0
  ),
  searchable_experiences as materialized (
    select id, lower(concat_ws(' ', title, annotation)) as searchable_text
    from public.experiences
  )
  insert into public.experience_tags_new (catalog_id, experience_id)
  select distinct c.id, e.id
  from common_categories c
  cross join searchable_experiences e
  where exists (
      select 1
      from unnest(c.lexical_triggers) as trigger(value)
      where btrim(trigger.value) <> ''
        and position(lower(btrim(trigger.value)) in e.searchable_text) > 0
    )
  on conflict do nothing;
end;
$function$;

revoke execute on function public.sync_experience_catalog_links_v1() from public, anon, authenticated;

-- Run when you need to update links:
-- select public.sync_experience_catalog_links_v1();
