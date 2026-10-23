-- Integration regression check. All fixture changes are rolled back.
begin;
do $test$
declare
  e integer := -2147483001;
  missing integer := -2147483002;
  concurrent integer := -2147483003;
  outside_scope integer := -2147483004;
  fixture_tag integer := -2147483001;
  seen integer[];
  failed_as_expected boolean := false;
  changed integer;
begin
  if exists (select 1 from public.experiences where id between -2147483004 and -2147483001)
     or exists (select 1 from public.tag_category_catalog where id between -2147483004 and -2147483001)
     or exists (select 1 from public.tags where citytag_id = fixture_tag)
     or exists (select 1 from public.tripster_tags where id = fixture_tag)
     or exists (select 1 from public.tripster_city_tags where citytag_id = fixture_tag) then
    raise exception 'Fixture IDs already occupied';
  end if;
  insert into public.experiences (id, title, type, status, city_id, last_synced_at)
  overriding system value values
    (e, 'refresh_v2_fixture_match', 'private', 'active', 149, now() - interval '1 day'),
    (missing, 'refresh_v2_missing', 'private', 'active', 149, now() - interval '1 day'),
    (concurrent, 'refresh_v2_concurrent', 'private', 'active', 149, now() + interval '1 hour'),
    (outside_scope, 'refresh_v2_other_scope', 'private', 'active', null, null);
  insert into public.tripster_tags (id, name) values (fixture_tag, 'Fixture');
  insert into public.tripster_city_tags (citytag_id, tag_id, city_id, experience_count)
    values (fixture_tag, fixture_tag, 149, 1);
  insert into public.tags (id, citytag_id, city_id) values (fixture_tag, -2147483005, 149);
  insert into public.tag_category_catalog
    (id, main_slug, main_name, sub_slug, sub_name, source, tripster_tag_id, lexical_triggers)
  overriding system value values
    (-2147483001, 'refresh-v2-fixture', 'Fixture', 'refresh-v2-direct', 'Direct', 'fixture', fixture_tag, '{}'),
    (-2147483002, 'refresh-v2-fixture', 'Fixture', 'refresh-v2-lexical', 'Lexical', 'fixture', null, array['refresh_v2_fixture_match']),
    (-2147483003, 'refresh-v2-fixture', 'Fixture', 'refresh-v2-classified', 'Classified', 'fixture', null, '{}'),
    (-2147483004, 'refresh-v2-fixture', 'Fixture', 'refresh-v2-stale', 'Stale', 'fixture', null, '{}');
  insert into public.tag_experience_table (experience_id, tags) values (e, 'refresh-v2-classified');
  insert into public.experience_tags_new (catalog_id, experience_id) values (-2147483004, e), (-2147483004, missing);
  insert into public.experience_tags (experience_id, citytag_id) values (missing, -2147483005);
  perform public.upsert_tripster_tag_bridge_v2(jsonb_build_array(jsonb_build_object(
    'id', fixture_tag, 'citytag_id', fixture_tag, 'city_id', 149, 'name', 'Fixture updated')));
  if not exists (select 1 from public.experience_tags where experience_id=missing and citytag_id=fixture_tag)
    or not exists (select 1 from public.tags where citytag_id=fixture_tag and name='Fixture updated') then
    raise exception 'Synthetic citytag migration must preserve legacy links';
  end if;

  perform public.replace_tripster_relations_v2(array[e],
    jsonb_build_array(jsonb_build_object('experience_id', e, 'citytag_id', fixture_tag)),
    jsonb_build_array(jsonb_build_object('experience_id', e, 'position', 0, 'medium_url', 'https://example.invalid/fixture.jpg')));
  begin
    perform public.replace_tripster_relations_v2(array[e],
      jsonb_build_array(jsonb_build_object('experience_id', e, 'citytag_id', -2147483009)), '[]');
  exception when foreign_key_violation then
    failed_as_expected := true;
  end;
  if not failed_as_expected or not exists (select 1 from public.experience_tags where experience_id=e and citytag_id=fixture_tag)
    or not exists (select 1 from public.experience_photos where experience_id=e) then
    raise exception 'Failed replacement must preserve the previous relations and photos';
  end if;

  perform public.sync_experience_catalog_links_v2(array[e]);
  if (select count(*) from public.experience_tags_new where experience_id=e) <> 3
    or exists (select 1 from public.experience_tags_new where experience_id=e and catalog_id=-2147483004) then
    raise exception 'Direct/lexical/classifier links or stale-link removal failed';
  end if;
  update public.experiences set title='refresh_v2_no_match' where id=e;
  perform public.replace_tripster_relations_v2(array[e], '[]', '[]');
  perform public.sync_experience_catalog_links_v2(array[e]);
  if (select count(*) from public.experience_tags_new where experience_id=e) <> 1
    or not exists (select 1 from public.experience_tags_new where experience_id=e and catalog_id=-2147483003) then
    raise exception 'Removed tags/triggers must disappear while saved classifier tags survive';
  end if;
  select array_agg(id) into seen from public.experiences where id not in (missing, concurrent, outside_scope);
  changed := public.finalize_tripster_catalog_refresh_v2(array[149], seen, now());
  if changed <> 1 or (select status from public.experiences where id=missing) <> 'inactive'
    or exists (select 1 from public.experience_tags_new where experience_id=missing)
    or exists (select 1 from public.experience_tags where experience_id=missing)
    or (select status from public.experiences where id=concurrent) <> 'active'
    or (select status from public.experiences where id=outside_scope) <> 'active'
    or (select status from public.experiences where id=e) <> 'active' then
    raise exception 'Missing/scope/concurrent-import reconciliation failed';
  end if;

  insert into public.experiences (id, title, type, status, city_id, last_synced_at)
  overriding system value
  select -2147483300 + n, 'refresh_v2_batch_fixture', 'private', 'active', 149, now()-interval '1 day'
  from generate_series(1, 101) as t(n);
  select array_agg(id) into seen from public.experiences
  where id not between -2147483299 and -2147483199;
  if public.finalize_tripster_catalog_refresh_v2(array[149], seen, now()) <> 100
    or public.finalize_tripster_catalog_refresh_v2(array[149], seen, now()) <> 1
    or public.finalize_tripster_catalog_refresh_v2(array[149], seen, now()) <> 0 then
    raise exception 'Deactivation must be bounded, resumable, and terminate with zero';
  end if;
end;
$test$;
rollback;
select 'refresh_v2_integration_checks_passed' as result;
