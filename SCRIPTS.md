# Scripts

Краткая памятка по локальным скриптам проекта. Нужна, чтобы быстро восстановить контекст, если история чата потеряется.

## Общие настройки

Все основные скрипты читают `.env` из корня проекта:

- `SUPABASE_URL` - URL проекта Supabase.
- `SUPABASE_SERVICE_ROLE_KEY` - service role key для записи через REST API.
- `TRIPSTER_TOKEN` - Bearer token Tripster.
- `TOKENATOR_API_KEY` - ключ Tokenator для генерации `description_new_html`.
- `CODA_API_KEY` - ключ Coda router.
- `APICLAUDE_API_KEY` - ключ ApiClaude router.

Важно:

- `raw_*` payload сейчас не пишем, чтобы не раздувать Supabase free tier.
- В `experience_photos` пишем первые 5 фото на экскурсию: `medium_url` и `thumbnail_url`. Если API вернул меньше пяти, сохраняем все доступные фотографии.
- `description_html` - исходное детальное описание Tripster.
- `description_new_html` - новое структурированное HTML-описание, сгенерированное LLM.

## `import_tripster_citytags.py`

Импортирует citytags из Tripster:

`https://experience.tripster.ru/api/partners/trvl/citytags/?city=<city_id>`

Пишет в таблицы:

- `tripster_tags`
- `tripster_city_tags`

Использует пагинацию `next`. Заполняет флаги тегов: `is_common`, `is_auto`, `is_seo_only`, `is_attraction`, `is_hidden`, и другие поля справочника.

Примеры:

```powershell
python import_tripster_citytags.py --city-slug Kaliningrad
python import_tripster_citytags.py --workers 10
```

## `tag_category_linker.py`

Готовит и применяет связи `tags.citytag_id -> tag_category_catalog.id`.

Важное правило: конкретные места и достопримечательности не разносятся в категории. Скрипт помечает их как `place`, чтобы разбирать отдельно.

Пайплайн:

```powershell
python tag_category_linker.py export --city-slug Kaliningrad --out kaliningrad_tag_category_candidates.csv
python tag_category_linker.py llm --input kaliningrad_tag_category_candidates.csv --out kaliningrad_tag_category_proposals.csv --limit 50 --workers 5
python tag_category_linker.py apply --input kaliningrad_tag_category_proposals.csv --city-id 149 --dry-run
python tag_category_linker.py apply --input kaliningrad_tag_category_proposals.csv --city-id 149
```

Пакетный LLM-прогон отправляет 10-20 тегов в одном запросе:

```powershell
python tag_category_linker.py llm-batch --input unique_category_candidates_for_llm.csv --out unique_category_candidates_llm_batch.csv --only-kind category_candidate --batch-size 15 --workers 8 --timeout 45 --max-retries 1
```

Для 1818 универсальных тегов при `--batch-size 15` получается примерно 122 LLM-запроса вместо 1818.

LLM размечает и универсальные теги, и конкретные места. Для мест она должна выбирать тип места из каталога, например `Куршская коса` -> `Природа и пейзажи / Заповедники`, но итог всё равно пишется только после ручного `approve=да`. Ввод передается структурированным JSON: `task`, `tag`, `allowed_categories`, `required_output_json`. Ожидаемый ответ: `tag_kind`, `is_reusable`, `selected_categories`.

Тест 30 потоков:

```powershell
python tag_category_linker.py llm --input kaliningrad_tag_category_candidates.csv --out kaliningrad_places_llm_test30w.csv --only-kind place --limit 30 --workers 30
```

На ApiClaude 30 потоков могут давать `429` и timeout; практичнее начинать с `--workers 5-10`.

Текущие LLM-настройки:

- основной провайдер: `LLM_API_BASE=https://modelhub.my/v1`, `LLM_MODEL=gpt-5.5`
- резерв: `LLM_FALLBACK_API_BASE=https://openrouter.ai/api/v1`, `LLM_FALLBACK_MODEL=deepseek/deepseek-v4-pro`

Проверка `gpt-5.5-xhigh-fast` на ModelHub вернула `404 Not Found`, рабочее имя модели там было `gpt-5.5`.
OpenRouter + `deepseek/deepseek-v4-pro` на тесте 30 place-тегов в 30 потоков отработал без ошибок.
ApiClaude для `tag_category_linker.py` не используется.

Для переиспользования утвержденных универсальных тегов по всем городам:

```powershell
python tag_category_linker.py apply --input kaliningrad_tag_category_proposals.csv --reuse-global --dry-run
```

Перед применением нужно проставить `approve=да` только у проверенных строк. LLM пишет предложения, но не считается источником истины.

## `tag_experience_classifier.py`

Размечает экскурсии из `experiences` по существующим `tag_category_catalog.sub_slug`.

На старте скрипт получает уникальные `sub_slug` из `tag_category_catalog` только для строк с `is_common=true`, передает их в LLM через запятую, отправляет экскурсии батчами и пишет результат в `tag_experience_table`.

Для каждой экскурсии также получает common-теги из старого Tripster-каталога по цепочке `experience_tags -> tripster_city_tags -> tripster_tags`, только с `tripster_tags.is_common=true`. Они передаются в LLM как `tripster_common_tags` и используются только как вспомогательный сигнал.

По умолчанию включен закрытый режим: LLM выбирает только существующие `sub_slug` и не придумывает новые теги/места. В этом режиме `new_tags` и `places_tags` пишутся пустыми строками. Для старого расширенного режима используйте `--allow-new-and-places`.

- `tags` - подходящие существующие `sub_slug` через запятую
- `experience_id` - `experiences.id`
- `title` - название экскурсии
- `new_tags` - подходящие негеографические теги/категории, которых пока нет в каталоге
- `places_tags` - теги конкретных мест и достопримечательностей, например `Куршская коса`, `Светлогорск`, `Балтийское море`, `Танцующий лес`, `высота Эфа`

Дополнительно скрипт пишет нормализованные связи существующих категорий в `experience_tags_new`:

- `catalog_id` - `tag_category_catalog.id`
- `experience_id` - `experiences.id`

При перезаписи экскурсии через `--include-processed` связи в `experience_tags_new` для этой экскурсии сначала удаляются, потом создаются заново.

Дефолтный LLM-провайдер: `https://api.aigate.shop/v1`, модель `anthropic/claude-haiku-4.5`, ключ берется из `LLM_API_KEY`.

`--limit` задает общий лимит экскурсий за запуск, `--batch-size` задает сколько экскурсий отправлять в один LLM-запрос. По умолчанию `--batch-size 3`.
`--workers` задает количество параллельных LLM-батчей. Например, `--workers 10` запускает до 10 запросов одновременно.
`--use-prompt-cache` включает кэширование компактного статического блока со списком `sub_slug` у LLM-провайдера. На `aigate.shop` проверено: первый запрос создает кэш, повторные запросы получают `cached_tokens`.
По умолчанию используется модель `anthropic/claude-haiku-4.5` с Anthropic-style thinking и бюджетом `4096` токенов. Если модель переопределить на OpenAI-совместимую, для нее отправляется `reasoning={"enabled":true,"effort":"high"}`. Для сравнения или более быстрого/дешевого запуска используйте `--no-reasoning`.
Строки, которые уже есть в `tag_experience_table`, пропускаются. Для принудительной перезаписи используйте `--include-processed`.

В логах для каждого LLM-запроса выводятся токены, cache hit/create и `cost_usd`. Каждая обработанная экскурсия выводится отдельно: `experience_id`, название, число существующих тегов, новых тегов и тегов мест. После записи выводится количество строк, созданных в `experience_tags_new`.

Примеры:

```powershell
python tag_experience_classifier.py --dry-run
python tag_experience_classifier.py --limit 30 --batch-size 3
python tag_experience_classifier.py --limit 100 --batch-size 5 --workers 10 --use-prompt-cache
python tag_experience_classifier.py --limit 30 --no-reasoning
python tag_experience_classifier.py --limit 30 --allow-new-and-places
python tag_experience_classifier.py --experience-id 123,456,789 --include-processed
```

## `propose_tripster_catalog_links.py`

Готовит CSV-кандидаты на однонаправленные связи `tag_category_catalog.tripster_tag_id -> tripster_tags.id` для старых тегов, встречающихся минимум в трёх городах. Сначала ищет точные slug, затем совпадения после нормализации вариантов транслитерации (`peshehodnyie` → `peshehodnye`), затем один лучший похожий slug через string similarity. Похожие slug не применяются автоматически.

Перед первым применением выполните [add_tripster_tag_id_to_catalog.sql](add_tripster_tag_id_to_catalog.sql) в Supabase SQL Editor. Он добавляет nullable FK `tripster_tag_id`: строка каталога связана максимум с одним старым Tripster-тегом, но один Tripster-тег может быть назначен нескольким дублирующимся категориям в разных родительских разделах. `--apply-exact` записывает только точные и нормализованные совпадения; `similar_slug` остаются в CSV для ручной проверки.

```powershell
python propose_tripster_catalog_links.py --out tripster_catalog_slug_proposals.csv
python propose_tripster_catalog_links.py --min-cities 5 --min-slug-similarity 0.80
python propose_tripster_catalog_links.py --apply-exact
```

## `compare_experience_tag_models.py`

Read-only сравнение LLM-классификации на одной случайной экскурсии. Скрипт не пишет ни в одну таблицу Supabase. По умолчанию проверяются только `anthropic/claude-haiku-4.5`, `anthropic/claude-sonnet-4.6`, `anthropic/claude-opus-4.8`. Остальной набор (`openai/gpt-5.4-mini`, `openai/gpt-5.5`, `deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro`, `google/gemini-3.1-pro-preview`, `z-ai/glm-5.2`) подключается через `--include-extra-models`; `--model` заменяет любой дефолтный набор. По умолчанию выбраны `no-cache`, `no-commons`, `embedding-no-commons` и `embedding-commons`. Первые два используют полный каталог; embedding-режимы через `match_tag_category_catalog_embeddings` выбирают 120 наиболее подходящих категорий. Режимы `no-commons` и `embedding-no-commons` полностью исключают `tripster_common_tags` из prompt, а `embedding-commons` передаёт их как подсказку. Чтобы добавить режимы, укажите `--mode cache --mode reasoning`. 

Режимы по умолчанию:

- `cache` — prompt cache включен, reasoning выключен;
- `no-cache` — без prompt cache и без reasoning;
- `reasoning` — без кэша, в запрос передаётся `reasoning={"enabled": true, "effort": "high"}` для любой модели.

В таблице есть старые common-теги Tripster и их количество, а также общее число всех Tripster-категорий экскурсии: связи берутся из `experience_tags`, а флаг `is_common=true` проверяется у связанного тега в `tripster_tags`. Аннотация экскурсии открывается кнопкой в модальном окне. Также выводятся пересечение тегов всех успешно ответивших моделей и его количество. Для каждой модели сначала объединяются результаты её режимов, после чего берётся пересечение между моделями.

В отчёт также добавляется локальная модель `lexical_triggers`: она не делает LLM-запросов, а сопоставляет нормализованные `title`, `tagline` и `annotation` с подстроками из `tag_category_catalog.lexical_triggers`. Интерфейс показывает её результаты отдельной строкой и число тегов, общих между пересечением LLM-моделей и `lexical_triggers`.

Для LLM-анализа и embedding-поиска выбранная экскурсия передаётся с полями `title`, `tagline` и `annotation`.

Для дефолтного набора это 12 LLM-запросов: `1 экскурсия × 3 модели × 4 режима`, плюс один embedding-запрос для выбора top‑120 категорий. LLM-запросы запускаются параллельно; при необходимости ограничить нагрузку используйте, например, `--workers 20`. Чтобы проверить все 9 моделей, добавьте `--include-extra-models`.

После прогона создаётся и открывается интерактивный `compare_experience_tag_models_report.html`: есть фильтры моделей и режимов, переключение между таблицей и карточками и кнопка «Копировать body» у каждого запроса. Общие теги пересчитываются с учётом включённых фильтров. Чтобы только создать файл, используйте `--no-open`; путь меняется через `--html-out`.

Для случайной экскурсии скрипт не загружает всю таблицу `experiences`: он получает минимальный и максимальный ID, выбирает случайный ID в этом диапазоне и точечно запрашивает ближайшую существующую экскурсию.

Пример сравнения двух моделей:

```powershell
python compare_experience_tag_models.py --model anthropic/claude-haiku-4.5 --model openai/gpt-5-mini --seed 42
```

Повторить те же пять экскурсий можно по `--seed`; для заданных ID используйте `--experience-id 123,456,789,101,102`. Ограничить режимы: `--mode cache --mode reasoning`.

## `import_tripster_experiences.py`

Импортирует список экскурсий по городам из Tripster:

`https://experience.tripster.ru/api/web/v2/experiences/`

Пишет в таблицы:

- `experiences`
- `tripster_tags`, `tripster_city_tags` — актуальный справочник Tripster
- `experience_tags`
- `experience_photos`
- `tags` — совместимый справочник для существующего FK `experience_tags.citytag_id`
- `tag_category_catalog`, `tag_category_links` — локальные категории
- `experience_tags_new` — пересобранные связи с новым каталогом

Что важно:

- Поддерживает пагинацию через `next`.
- Работает по городам.
- Обычный запуск без `--photos-only` выполняет полное обновление выбранных городов, включая города с нулевым `cities.experience_count`. `--limit` — размер страницы, а не лимит обновления.
- Сначала полностью загружает citytags и экскурсии города, сверяет число записей с API `count`, проверяет циклы пагинации. У citytags `count` может включать дубли одного `citytag_id`: полнота проверяется по числу полученных строк, а справочник записывается без дублей. У экскурсий `count` иногда превышает фактический набор даже после последней страницы. При таком расхождении весь список читается повторно: запись допускается только при одинаковом `count` и полностью совпавших наборах ID двух чтений. Пустой ответ с ненулевым `count`, ошибка страницы или изменившийся набор ID блокируют запись города и последующую деактивацию.
- Обновляет канонические `tripster_tags` / `tripster_city_tags`. Пропавшие городские теги сохраняет для ссылочной целостности с `experience_count=0`, `is_hidden=true`.
- `upsert_tripster_tag_bridge_v2()` обновляет совместимый справочник `tags` по паре `(city_id, id)`: заменяет временные `citytag_id` актуальными, сохраняя связи через `ON UPDATE CASCADE`. Это учитывает частичный уникальный индекс старого справочника.
- Связи `experience_tags` и фотографии заменяет транзакционно, пакетами по 100 экскурсий. Для перекрывающихся экскурсий использует блокировки в порядке ID. Одновременно записывает до четырёх городов.
- После успешной загрузки всех выбранных городов вызывает `sync_tripster_local_catalog_v2()` только для метаданных категорий, затем пакетами `sync_experience_catalog_links_v2(ids)`. Последняя пересобирает `experience_tags_new`: прямые сопоставления Tripster, связи из `tag_category_links`, совпадения lexical triggers и сохранённые решения из `tag_experience_table`. Остальные старые связи в обновляемой области удаляются; отдельные ручные связи без записи об источнике не сохраняются. Старый аддитивный `sync_tripster_local_categories_v1()` сохранён как совместимая обёртка, но полный импорт его не вызывает: общий INSERT всех связей превышал REST statement timeout.
- Скрывает автоматически созданные локальные категории, у которых больше нет подходящих тегов Tripster с экскурсиями; строки и URL категорий сохраняются.
- После пересборки связей `finalize_tripster_catalog_refresh_v2()` помечает отсутствующие экскурсии как `status='inactive'` и удаляет их старые и новые связи тегов. Один вызов обрабатывает не более 100 записей; скрипт повторяет вызовы до результата `0` и сохраняет накопленный `inactive_count` в checkpoint. Сами карточки, отзывы и фотографии сохраняются. Область — только выбранные города и типы `private,group,ticket,transfer_service`; присутствие учитывается по всем ответам выбранных городов. `exp_status` остаётся значением из последнего ответа API.
- Возвращённая Tripster экскурсия получает актуальный `status` из API при следующей загрузке. `last_synced_at` обновляется; карточки, синхронизированные после старта параллельным импортом, не деактивируются.
- При ошибке любого города финальная пересборка каталога и деактивация пропускаются, процесс завершается с кодом 1. Уже записанные города сохраняются; для завершения нужно повторить тот же запуск.
- Полный запуск сохраняет `tripster_refresh_checkpoint.json`: исходное время старта, область городов, ID экскурсий успешно записанных городов, завершение синхронизации метаданных и прогресс пакетов новых связей. Для продолжения незавершённого обновления используйте те же фильтры с `--resume`. Уже завершённые шаги пропускаются, финальная деактивация учитывает объединённое присутствие из всех частей запуска. `--checkpoint` позволяет выбрать другой файл; завершённую точку восстановления повторно использовать нельзя.
- `--incomplete-only` обновляет выбранные неполные города и связи, но не деактивирует отсутствующие экскурсии.
- `--dry-run` выполняет чтение API и проверки полноты без записей и вызовов RPC.
- Сохраняет ранее загруженные детальные поля, если список Tripster их не передал.
- `MAX_PHOTOS_PER_EXPERIENCE = 5`: применяется и при полном обновлении, и в режиме `--photos-only`.
- `raw_list_payload`, `raw_detail_payload`, `raw_payload` оставлены закомментированными.
- `experience_tags` хранит связь `experience_id` + `citytag_id`.
- Если тег из экскурсии не найден в citytags данного города, создаётся synthetic tag с отрицательным `citytag_id` в каноническом и совместимом справочниках.
- Для обновления только фото используйте `--photos-only`: скрипт трогает только `experience_photos`, без upsert `experiences`, `tags` и `experience_tags`. Отдельный файл не нужен. Режим заменяет сохранённые фотографии первыми пятью из ответа API для уже существующих экскурсий. Сохраняются ссылки, сами файлы изображений не скачиваются.

Примеры:

```powershell
python import_tripster_experiences.py --city-slug Kaliningrad --workers 10
python import_tripster_experiences.py --incomplete-only --workers 12 --limit 100
python import_tripster_experiences.py --photos-only --workers 5 --timeout 120
```

Для новой базы сначала выполните `refresh_tripster_catalog_v2.sql` и актуальный `sync_tripster_local_categories_v1.sql` в Supabase SQL Editor. В текущем проекте функции установлены. RPC разрешены только `service_role`; перед записью скрипт проверяет доступность новых функций пустыми вызовами. Предыдущий аддитивный `sync_experience_catalog_links_v1()` для полного обновления не используется.

Проверки изменений:

```powershell
python -m unittest test_import_tripster_experiences -q
python import_tripster_experiences.py --city-slug Kaliningrad --workers 1 --dry-run
```

`verify_tripster_catalog_refresh_v2.sql` содержит интеграционную проверку SQL-функций с тестовыми записями и обязательным `ROLLBACK`.

## `import_tripster_experience_details.py`

Обогащает уже импортированные экскурсии детальными полями Tripster:

`https://experience.tripster.ru/api/web/v2/experiences/<id>/?detailed=true`

Обновляет только `experiences`.

Поля:

- `description_html`
- `meeting_point_text`
- `finish_point_text`
- `video_id`
- `photos_count`
- `cover_image_url`
- `cover_image_source`
- `schedule`
- `schedule_text`
- `languages`
- `legality_description`
- `options`
- `integration`
- `detail_synced_at`

Не трогает:

- `experience_tags`
- `experience_photos`
- `raw_detail_payload`

По умолчанию берет только строки без `description_html`. С `--all` перезаписывает уже заполненные.

Примеры:

```powershell
python import_tripster_experience_details.py --city-slug Kaliningrad --workers 20
python import_tripster_experience_details.py --id 24667 --workers 1
python import_tripster_experience_details.py --city-slug Kaliningrad --all --workers 20
```

## `import_tripster_reviews.py`

Импортирует последние отзывы Tripster по экскурсиям:

`https://experience.tripster.ru/api/partners/<partner>/experiences/<experience_id>/reviews/`

По умолчанию:

- `partner = trvl`
- `limit = 10` последних отзывов на экскурсию
- `workers = 10`

Для загрузки по 10 последних отзывов всех активных экскурсий:

```powershell
python import_tripster_reviews.py --active-only --limit 10 --workers 5 --timeout 120
```

`--active-only` выбирает `experiences.status = 'active'`. Если отзывов меньше десяти, сохраняются доступные. Записи обновляются по ID, ранее сохранённые старые отзывы не удаляются. Прогресс записывается в `tripster_reviews_checkpoint.json` после успешной записи пачки; для продолжения добавьте `--resume` с теми же параметрами и областью выбора. Ошибки отдельных экскурсий оставляют их незавершёнными для повторной загрузки.

Пишет в таблицу:

- `experience_reviews`

Поля таблицы:

- `id` - Tripster review id, primary key.
- `experience_id` - ссылка на `experiences.id`.
- `author_name`
- `avatar_small_url`
- `avatar_medium_url`
- `created_at`
- `created_on`
- `created_on_dt`
- `rating`
- `text`
- `guide_reply`
- `traveler_level`
- `experience_title`
- `experience_url`
- `experience_is_bookable`
- `experience_is_visible`
- `experience_type`
- `experience_format`
- `photos` - упрощённый JSON со ссылками фото отзыва.
- `imported_at`
- `updated_at`

`raw_payload` не пишется.

Примеры:

```powershell
python import_tripster_reviews.py --city-slug Kaliningrad --limit 10 --workers 10
python import_tripster_reviews.py --id 24330 --limit 10 --workers 1
python import_tripster_reviews.py --city-slug Kaliningrad --max-rows 1 --limit 10 --dry-run
```

## `enrich_tripster_descriptions.py`

Генерирует новое структурированное описание из `description_html` и пишет его в:

- `experiences.description_new_html`

Берет только строки, где `description_html` не пустой. По умолчанию пропускает уже заполненные `description_new_html`; с `--all` перегенерирует.

Prompt просит HTML-фрагмент с разделами:

- Мини-описание
- Кому подойдет
- Что увидим
- Маршрут/программа
- Что включено
- Что не включено / доп. расходы / опции с ценами
- Физическая нагрузка
- Что взять с собой
- Место сбора
- FAQ

Из дополнительных полей в prompt сейчас подставляются:

- `meeting_point_text`
- `finish_point_text`
- `price_included_description`
- `price_not_included_description`

`options` специально убраны из prompt, потому что туда попадала базовая цена экскурсии и модель ошибочно относила ее к допрасходам.

Провайдеры:

- `tokenator` - по умолчанию, модель `gemini-3.5-flash`.
- `coda` - Coda router, по умолчанию `claude-sonnet-4.6`, можно ставить `claude-opus-4-8`.
- `apiclaude` - `https://apiclaude.ooguy.com/v1/chat/completions`, по умолчанию `claude-opus-4-8`.

Coda stream читается байтами и явно декодируется как UTF-8. Это исправляет mojibake вида `ÐÐ¸Ð½Ð¸...`.

Скрипт выводит успешные id:

```text
[7/30] 24330: OK
```

И финальную скорость:

```text
Done: imported=30, errors=0, elapsed=190.2s, speed=9.46/min, avg=6.34s/row
```

Примеры:

```powershell
python enrich_tripster_descriptions.py --city-slug Kaliningrad --max-rows 5 --workers 2
python enrich_tripster_descriptions.py --provider tokenator --city-slug Kaliningrad --max-rows 30 --workers 3
python enrich_tripster_descriptions.py --provider coda --model claude-opus-4-8 --city-slug Kaliningrad --max-rows 20 --workers 20
python enrich_tripster_descriptions.py --provider apiclaude --city-slug Kaliningrad --max-rows 5 --workers 1
python enrich_tripster_descriptions.py --provider tokenator --all --id 24330 --workers 1
```

Практические наблюдения:

- Tokenator стабильнее для небольших параллельных запусков.
- Coda с `claude-opus-4-8` проходила 10-20 потоков, но были проблемы со stream/кодировкой до фикса.
- ApiClaude в 10 потоков давал read timeout, лучше запускать 1-3 потока.

## `count_tripster_city_counts.py`

Быстрый count-only скрипт. По каждому городу делает один запрос с `limit=1`, берет `count` из Tripster и ничего не пишет в Supabase.

Нужен для сверки реального количества экскурсий по городам.

Выходные файлы:

- `tripster_city_counts_complete.json`
- `tripster_city_counts_complete.csv`

Примеры:

```powershell
python count_tripster_city_counts.py
python count_tripster_city_counts.py --city-slug Kaliningrad
```

## `count_tripster_experiences.py`

Более тяжелый count-only скрипт. Ходит по страницам Tripster и считает:

- количество полученных строк;
- уникальные `experience_id`;
- расхождение с `count`.

Ничего не пишет в Supabase.

Использовать, когда нужно проверить пагинацию и реальные уникальные id, а не только поле `count`.

Примеры:

```powershell
python count_tripster_experiences.py --city-slug Kaliningrad
python count_tripster_experiences.py --workers 10
```

## `populate_catalog_seo_fields.py`

Генерирует через AIGate (`https://api.aigate.shop/v1`) SEO-шаблоны для строк `tag_category_catalog` и пишет поля:

- `title`
- `header`
- `seo_description`
- `seo_text`

Модель по умолчанию: `anthropic/claude-sonnet-5`, ключ берется из `LLM_API_KEY`.

По умолчанию скрипт берет строки, где `seo_text is null`. Prompt просит `title` с `{{months}}` перед `2026`, длиной 50-70 символов, максимум 80, а `seo_text` в 3 абзаца, чуть подробнее, желательно от 150 слов, с плейсхолдерами `{{city}}`, `{{count}}`, `{{reviews}}`, `{{months}}`, `{{min_price}}`. Перед `{{city}}` prompt запрещает ставить предлоги вроде `в`, `по`, `из`, потому что нужная форма города уже хранится в базе. Жесткой проверки объема, абзацев и плейсхолдеров нет; скрипт проверяет только, что LLM вернул строковые поля. На генерацию одной строки дается максимум 2 попытки. Параллельность задается через `--workers`, по умолчанию `10`. Чтобы игнорировать `seo_text` и обработать выбранные строки заново, используйте `-force`.

Примеры:

```powershell
python populate_catalog_seo_fields.py --dry-run --limit 1
python populate_catalog_seo_fields.py --catalog-id 1
python populate_catalog_seo_fields.py --sub-slug individualnye --dry-run
python populate_catalog_seo_fields.py --limit 50 --workers 10
python populate_catalog_seo_fields.py -force --limit 10
```

## `populate_location_seo_blocks.py`

Генерирует через AIGate SEO-блоки для страниц городов и стран и пишет их в таблицу:

- `seo_blocks`

Поля записи:

- `entity_type` - `city` или `country`.
- `entity_id` - id города или страны строкой.
- `slug` - технический slug вида `city__city__149` или `country__country__238`.
- `title`
- `h1`
- `h2`
- `body` - расширенный HTML с секциями `<h2>`, абзацами и списками.
- `faq` - JSON-текст с массивом вопросов и ответов, потому что текущая колонка `seo_blocks.faq` хранит строку.
- `is_active=true`

Для города скрипт подтягивает контекст из `cities`, популярных `tags` и топовых `experiences`: количество экскурсий, гидов, отзывы, точную минимальную цену среди активных экскурсий, темы и достопримечательности. Если в `.env` есть `TRIPSTER_TOKEN`, дополнительно читает Tripster city SEO endpoint и передает в LLM только структуру референса: title/description/h1/h2, список `<h2>`, счетчики, рейтинг и price ranges. Сам конкурентский HTML-текст в prompt не передается, чтобы не копировать его. Отключить можно через `--no-tripster-reference`.

Prompt просит рассказать об основных местах и форматах города, не копировать тексты конкурентов и не упоминать Tripster. Для городов целевой объем `body` - 1200-1800 слов, 10-14 секций; для стран - 1000-1600 слов, 8-12 секций. Городская статья должна начинаться с общей информации об экскурсиях в этом городе: какие форматы доступны, сколько маршрутов/гидов есть в контексте, кому подходят групповые, индивидуальные, пешие, автобусные или выездные программы, как ориентироваться по цене и отзывам. Только после этого текст переходит к достопримечательностям, районам, музеям, природе, гастрономии и окрестностям.

Для страны скрипт подтягивает контекст из `countries`, популярных городов и топовых `experiences` по стране.

В пакетном режиме без явного `--city-id`, `--city-slug` или `--country-id` скрипт заранее исключает сущности, у которых уже есть `seo_blocks.body`, поэтому повторный запуск с `--limit 5` берет следующие 5 необработанных городов или стран. Если блок для явно выбранного `entity_type + entity_id` уже есть и `body` заполнен, строка пропускается. Для перегенерации используйте `-force`. Запись делается логикой `PATCH` существующей строки или `POST` новой строки; уникальный индекс в базе не требуется.

Если LLM или Supabase дают ошибку по отдельной сущности, пакетный запуск не падает целиком: строка выводится как `failed ...`, остальные futures продолжают работу, а в конце печатается список ошибок. Повторный запуск без `-force` продолжит с необработанных сущностей.

Для снижения ошибок вида `Unterminated string` скрипт отправляет в LLM `response_format={"type":"json_object"}`, задает `max_tokens=20000` и делает 3 попытки генерации. Если JSON сломан или поле `body` не вернулось, повторная попытка просит полный JSON заново, но с более коротким `body` на 700-1000 слов. Если провайдер не поддерживает `response_format`, можно отключить его через `--no-response-format`.

Примеры:

```powershell
python populate_location_seo_blocks.py --city-id 149 --dry-run
python populate_location_seo_blocks.py --city-slug Kaliningrad
python populate_location_seo_blocks.py --entity country --country-id 238 --dry-run
python populate_location_seo_blocks.py --entity both --limit 10 --workers 4
python populate_location_seo_blocks.py --city-id 149 -force
python populate_location_seo_blocks.py --city-id 149 --no-tripster-reference --dry-run
python populate_location_seo_blocks.py --entity city --limit 100 --workers 5 --generation-attempts 4
```

## Текущее состояние по Калининграду

На момент последних запусков:

- `description_html` есть примерно у 861 экскурсий Калининграда.
- `description_new_html` было заполнено у 147 экскурсий.
- Остаток был около 714.

Перед массовым запуском лучше перепроверить актуальные числа запросом:

```sql
select
  count(*) filter (where description_html is not null) as with_source_description,
  count(*) filter (where description_new_html is not null) as with_new_description,
  count(*) filter (where description_html is not null and description_new_html is null) as remaining
from public.experiences
where city_id = 149;
```
