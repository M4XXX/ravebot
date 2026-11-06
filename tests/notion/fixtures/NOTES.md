# Notion API 2026-03-11 spike (scratch workspace, all objects trashed afterwards)

Fixtures `*_real.json` are real, sanitized payloads. `*_doc.json` stay hand-written.

- Create DB: `POST /v1/databases` {parent:{type:page_id}, title, initial_data_source:{properties}}; response has `data_sources[0].id`. Title prop must be declared (`{"title":{}}`), name is free (Events "ID", DJs "DJ name", Slots "Name"). New DBs get an extra "Default view".
- Relations: `{"relation":{"data_source_id":..,"dual_property":{"synced_property_name":..}}}`; `single_property:{}` is one-way. `limit: 1` is accepted but ignored (stays null). Create Slots with Event (dual) and DJ (single), then PATCH `/v1/data_sources/{id}` DJ to dual with name "Slots". Further props are added by PATCH data source; delete a prop with `null` (404 if it does not exist).
- SPEC CONFLICT: the Events reverse relation cannot be called "Slots" (Events has a Number "Slots"). Use "Slot rows".
- FLAG: Start stays text "HH:MM" (Notion has no time-of-day type). Slots needs a title property (spec lists none): "Name", set "Slot N".
- `prop("Event").first().prop("Start")` and `.map(current.prop(..))` fail ("Type error with formula"), so Slots needs rollup helpers (all work):
  `Event start` rollup(Event.Start, show_original), `Event slot length` rollup(Event."Slot length", max), `Event date src` rollup(Event.Date, latest_date), `Event date` formula `prop("Event date src")`. A rollup of a rollup is rejected, hence the formula hop.
- Time formula (verified 18:00/60/slot 3 => 20:00, 22:30/90/slot 3 => 01:30; output type string):
  `lets(m, mod(toNumber(prop("Event start").substring(0, 2)) * 60 + toNumber(prop("Event start").substring(3, 5)) + (prop("Slot number") - 1) * prop("Event slot length"), 1440), format(floor(m / 60)).padStart(2, "0") + ":" + format(m % 60).padStart(2, "0"))`
- Slot status: `if(empty(prop("DJ")), "Open", "Booked")` (verified Open/Booked).
- DJs rollups WORK, no bot-written fallback needed: `Events booked` rollup(Slots.Event, `unique`) -> number (2 slots in one event = 1, verified); `Last booked` rollup(Slots."Event date", latest_date) -> date (verified incl. event date change and clearing a slot). Rollups recalc within ~2 s.
- Pages: parent `{"type":"data_source_id","data_source_id":..}`. Relation write `{"DJ":{"relation":[{"id":..}]}}`, clear with `[]`. Same DJ in two slots of one event works. Read shape: title/rich_text/number/date/select as in `*_doc.json`.
- Differences to `*_doc.json`/models.py: real pages have `in_trash` and `is_archived` (no `archived` key); relation values carry `has_more`; formula/rollup props exist in Slots/DJs pages (rollup show_original is an array). Parsing in models.py is otherwise compatible.
- Query: `POST /v1/data_sources/{id}/query` {page_size, start_cursor, filter}. page_size > 100 is accepted (clamped, no error). Response `has_more`, `next_cursor`. Filters worked: title contains, relation contains, relation is_empty.
- Trash: `PATCH /v1/pages/{id}` {"in_trash":true}; `archived:true` is a 400 in this version. Trashed pages are excluded from queries; retrieve still returns them with `in_trash: true` (`is_archived` stays false). Trashing a database hides its rows and keeps GET working (`in_trash: true`); search results lag.
- Views: `POST /v1/views` needs `database_id` + `data_source_id`, `name`, `type:"table"`. `sorts[].property` accepts a name (stored as ID). `configuration.properties[]` needs `property_id` (name accepted); unlisted props become hidden. `group_by.property_id` needs the real (URL-decoded, e.g. `@~pK`) ID; a name gives 400. Working payloads in `views_real.json`:
  Events `sorts:[{Date,descending}]`; DJs `sorts:[{Last booked,descending}]` + properties list; Slots `sorts:[{Slot number,ascending}]`, `configuration:{type:table, group_by:{type:relation, property_id:<Event id>, sort:{type:descending}}, properties:[Slot number, Time, DJ]}`.
- List views: `GET /v1/views?database_id=<id>` (or `data_source_id`) returns only `{id}` items (+has_more/next_cursor); `GET /v1/views/{id}` for the name. Each new DB already has "Default view"; match by name "Overview"/"Lineup"/"All DJs".
- Group order: the view response only echoes `group_by.sort.type: descending`, it does not reveal the group order. Newest-first by event ID title is NOT verifiable through the API (two events on one date only; check in the Notion UI).
- Errors: 400 `{"object":"error","status":400,"code":"validation_error","message":..}`; 404 `code: object_not_found` with `additional_data.integration_id`. Formula errors only say "Type error with formula".
- Rate limits: 60 requests over 20 threads gave no 429 and no rate-limit headers (documented average ~3 req/s, then 429 + Retry-After; not triggered). The existing Retry-After handling in api.py stays valid.
- Integration needs: access to the parent page (shared), content read/insert/update. Creating databases and views worked with those.
