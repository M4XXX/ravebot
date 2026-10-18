# RaveBot

Discord bot that automates DJ booking for recurring online rave events — onboarding, self-service slot booking, proactive outreach, and admin oversight, all via Discord slash commands and button-driven DM flows with Notion as the event database.

---

## The Problem

Running a recurring online rave means coordinating DJ lineups every single week: chasing availability, confirming bookings, filling last-minute gaps, and keeping everyone informed. Done manually, it's a full-time side job. RaveBot makes it automatic.

---

## How It Works

### Step 1 — A DJ joins

When someone receives the "DJ" role in your Discord server, RaveBot DMs them automatically: *"Hey! I'm RaveBot — I handle all DJ bookings here. Want me to add you to the booking pool?"* They fill in a quick onboarding modal (DJ name, VRChat name, logo, stream links) and are ready to book. No admin involvement needed.

### Step 2 — They book a slot

Any time a DJ wants to play, they DM RaveBot. A button-driven menu guides them through picking an event and a slot in seconds. Booking is race-condition safe — two DJs can't grab the same slot simultaneously. Cancellations work the same way.

### Step 3 — Gaps get filled automatically

Before each event, RaveBot scans for open slots and DMs eligible DJs who haven't been contacted recently. It respects configurable cooldowns so DJs don't get spammed. Day-before reminders go out to everyone who is booked.

### Step 4 — Admin sees everything

A live lineup post in your DJ channel updates automatically whenever Notion changes. Use `/ravebot-status` for a quick embed showing slot assignments for the next event. Every bot↔DJ DM is mirrored to an admin log channel. For edge cases, admins can take over a DM conversation or force-book any slot.

---

## Screenshots

**DJ booking flow — button-driven DM interface**

![DJ booking flow](docs/assets/mockup-booking-flow.png)

**Lineup post — auto-updated when Notion changes**

![Lineup post](docs/assets/mockup-lineup-post.png)

**Admin /status — next event at a glance**

![Admin /status](docs/assets/mockup-admin-status.png)

---

## Features at a Glance

**For DJs**
- Automatic onboarding DM when "DJ" role is assigned
- Button-driven self-service booking and cancellation via DM
- Day-before event reminders
- Go active / go inactive at any time

**For Admins**
- Full DM transcript log in admin channel (both directions)
- `/intervene` to take over any DJ DM conversation
- `/admin book` and `/admin cancel` to force-manage any slot
- `/admin dj set-active` to add/remove DJs from the booking pool
- `/ravebot-status` for next-event overview (date, slots, DJ assignments)
- `/admin onboard` to invite a DJ who already had the role; `/admin onboard-preview` lists who is not registered yet
- `/list-djs` for full DJ roster with active/inactive status
- `/config` with 14 settings to tune every bot behaviour, including the passive/active switch
- `/add-event` to manually create a Notion event

**Automation**
- Optionally auto-creates future events in Notion (off by default; set a look-ahead count to enable)
- Every night starts at a configurable local time in a configurable time zone, all year round; its slots follow one after another, each exactly one slot duration long
- Auto-posts and updates the lineup embed whenever Notion changes
- Proactive outreach to fill open slots before each event
- Alembic database migrations run automatically at container start

**Safety defaults**
- Starts in **passive mode**: joins your server without contacting anyone until an organizer runs `/config set active on`
- Serves exactly one server (`GUILD_ID`); never deletes or archives anything in Notion
- Admin commands are hidden from members without Manage Server; mirrored DMs can never ping
- Startup setup check posts missing roles, role order and channel permissions to the log channel
- Reports (but never auto-fixes) drift between Notion and the booking database

---

## Slash Command Reference

| Command | Who | Description |
|---|---|---|
| `/config set dj-channel` | Admin | Channel where lineup posts are sent |
| `/config set log-channel` | Admin | Channel for admin alerts and DM transcripts |
| `/config set slots-per-event` | Admin | Slots of every event whose Notion `Slots` is empty, and of events the bot creates (default: 6) |
| `/config set event-interval-days` | Admin | Days between auto-created events (default: 7) |
| `/config set lookahead-count` | Admin | Future events to keep in Notion; 0 = automatic creation off (default: 0) |
| `/config set start-time` | Admin | First slot start time HH:MM in the configured time zone (default: 20:00) |
| `/config set timezone` | Admin | IANA time zone for the start time, e.g. Europe/Berlin (default: UTC) |
| `/config set slot-duration` | Admin | Slot length in minutes (default: 60) |
| `/config set outreach-days-before` | Admin | Days before event to begin outreach (default: 14) |
| `/config set outreach-cooldown-hours` | Admin | Hours between contacts per DJ (default: 12) |
| `/config set outreach-max` | Admin | Max DJs contacted per outreach cycle (default: unlimited) |
| `/config set event-schedule` | Admin | Cron expression, stored for a future scheduler (not used yet) |
| `/config set active` | Admin | `on` = contact DJs, run outreach/reminders/lineup; `off` = passive (default) |
| `/config set dm-log` | Admin | Mirror DJ DMs into the log channel (`on`, default) or keep them private (`off`) |
| `/config show` | Admin | All current settings and when each background loop last completed |
| `/intervene start` | Admin | Take over a DJ's DM conversation |
| `/intervene send` | Admin | Send a message as the bot in an active intervention |
| `/intervene end` | Admin | Return DM control to the bot |
| `/admin book` | Admin | Force-book a slot for any DJ |
| `/admin cancel` | Admin | Force-cancel any booking |
| `/admin dj set-active` | Admin | Toggle a DJ's active/inactive status |
| `/admin onboard` | Admin | Send the onboarding DM to one member with the DJ role |
| `/admin onboard-preview` | Admin | List members with the DJ role who are not registered (sends nothing) |
| `/ravebot-status` | Admin | Embed showing next event: slots, date, DJ assignments |
| `/list-djs` | Admin | Embed listing all registered DJs with status |
| `/add-event` | Admin | Manually create a Notion event for a specific date |

---

## Quick Links

[User Guide →](docs/user-guide.md) | [Developer Guide →](docs/developer-guide.md) | [Changelog →](CHANGELOG.md)

---

## Built With

[disnake](https://docs.disnake.dev/) · [asyncpg](https://magicstack.github.io/asyncpg/) · [httpx](https://www.python-httpx.org/) · [Notion API](https://developers.notion.com/) · PostgreSQL 16 · Docker · [uv](https://docs.astral.sh/uv/)
