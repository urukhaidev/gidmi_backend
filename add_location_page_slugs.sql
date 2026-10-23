create or replace function public.slugify_ru(input text)
returns text
language sql
immutable
strict
set search_path = ''
as $$
  select nullif(
    regexp_replace(
      regexp_replace(
        trim(both '-' from replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(lower(input), 'ё','e'), 'ж','zh'), 'х','h'), 'ц','c'), 'ч','ch'), 'ш','sh'), 'щ','sch'), 'ю','yu'), 'я','ya'), 'а','a'), 'б','b'), 'в','v'), 'г','g'), 'д','d'), 'е','e'), 'з','z'), 'и','i'), 'й','i'), 'к','k'), 'л','l'), 'м','m'), 'н','n'), 'о','o'), 'п','p'), 'р','r'), 'с','s'), 'т','t'), 'у','u'), 'ф','f'), 'ы','y'), 'э','e')),
        '[^a-z0-9]+', '-', 'g'
      ),
      '-+', '-', 'g'
    ),
    ''
  );
$$;

alter table public.countries
  add column if not exists page_slug text;

alter table public.cities
  add column if not exists page_slug text;

comment on column public.countries.page_slug is 'Human-readable route slug generated from the Russian country name.';
comment on column public.cities.page_slug is 'Human-readable route slug generated from the Russian city name. cities.slug remains the external Tripster slug.';

update public.countries
set page_slug = public.slugify_ru(name_ru)
where name_ru is not null;

with city_slugs as (
  select
    c.id,
    public.slugify_ru(c.name_ru) as base_slug,
    public.slugify_ru(co.name_ru) as country_slug,
    count(*) over (partition by public.slugify_ru(c.name_ru)) as duplicate_count
  from public.cities c
  left join public.countries co on co.id = c.country_id
  where c.name_ru is not null
)
update public.cities c
set page_slug = case
  when city_slugs.duplicate_count > 1 and city_slugs.country_slug is not null
    then city_slugs.base_slug || '-' || city_slugs.country_slug
  else city_slugs.base_slug
end
from city_slugs
where c.id = city_slugs.id;

create unique index if not exists countries_page_slug_unique
  on public.countries (page_slug)
  where page_slug is not null;

create unique index if not exists cities_page_slug_unique
  on public.cities (page_slug)
  where page_slug is not null;
