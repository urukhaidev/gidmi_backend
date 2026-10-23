-- One-to-one optional link from a new catalog category to one legacy Tripster tag.
-- Run this in the Supabase SQL editor before using --apply-exact.
alter table public.tag_category_catalog
    add column if not exists tripster_tag_id bigint;

alter table public.tag_category_catalog
    drop constraint if exists tag_category_catalog_tripster_tag_id_fkey;

alter table public.tag_category_catalog
    add constraint tag_category_catalog_tripster_tag_id_fkey
    foreign key (tripster_tag_id) references public.tripster_tags(id)
    on delete set null;

create index if not exists tag_category_catalog_tripster_tag_id_idx
    on public.tag_category_catalog(tripster_tag_id);
