<p align="center">
  <img src="docs/assets/banner.svg" alt="RaveBot: DJ booking on autopilot for your Discord rave" width="100%">
</p>

<p align="center">
  <a href="https://github.com/M4XXX/ravebot/releases"><img alt="Release" src="https://img.shields.io/github/v/release/M4XXX/ravebot?include_prereleases&style=flat-square&color=7c5cff&label=release"></a>
  <a href="https://github.com/M4XXX/ravebot/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/M4XXX/ravebot/ci.yml?branch=main&style=flat-square&label=tests"></a>
  <a href="https://github.com/M4XXX/ravebot/pkgs/container/ravebot"><img alt="Docker image" src="https://img.shields.io/badge/ghcr.io-m4xxx%2Fravebot-2ad4ff?style=flat-square&logo=docker&logoColor=white"></a>
  <a href="LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-blueviolet?style=flat-square"></a>
  <img alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-3776ab?style=flat-square&logo=python&logoColor=white">
  <img alt="disnake" src="https://img.shields.io/badge/discord-disnake-5865f2?style=flat-square&logo=discord&logoColor=white">
  <img alt="Notion" src="https://img.shields.io/badge/notion-API-000000?style=flat-square&logo=notion&logoColor=white">
  <img alt="PostgreSQL 16" src="https://img.shields.io/badge/postgresql-16-336791?style=flat-square&logo=postgresql&logoColor=white">
</p>

<p align="center">
  <b>Run a weekly online rave without chasing DJs.</b><br>
  RaveBot onboards DJs, lets them book slots by themselves, invites them when slots stay open,<br>
  keeps a live lineup in your Discord and tells you about everything that matters.
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-how-it-works">How it works</a> ·
  <a href="#-screenshots">Screenshots</a> ·
  <a href="#-commands">Commands</a> ·
  <a href="#-updating">Updating</a> ·
  <a href="docs/user-guide.md">User guide</a> ·
  <a href="docs/developer-guide.md">Developer guide</a>
</p>

---

## ✨ Highlights

| | |
|---|---|
| 🎧 **DJ onboarding by chat** | A DJ gets the DJ role and the bot asks five short questions in the DMs: name, VRChat name, logo (an image they just send), stream links. No forms, no popups. |
| 🗓️ **Self-service booking** | DJs book and cancel slots with buttons. Two DJs can never take the same slot. |
| 📨 **Automatic invites** | DJs in the *Active DJ Booking Group* get a message when slots stay open. Cooldowns keep it friendly, reminders go out the day before. |
| 📋 **Live lineup post** | A new *Lineup Update* appears in your DJ channel whenever the lineup changes, with a *Book a slot* button. |
| 🗂️ **Notion as the control room** | Events, slots and DJs live in three Notion databases the bot creates itself. Edit a slot in Notion and the bot syncs it. |
| 🛰️ **Guided setup** | `/setup` walks you through channels, roles, event defaults, time zone and invites. The bot stays idle until you press *Activate*. |
| 🔔 **Quiet admin log** | Only what matters: bookings, new DJs, problems and the DMs people send the bot, with a *Reply* button. |
| 🪐 **Admin page** | An optional password protected web page to add and remove admins without touching the container. |

---

## 🧭 How it works

```mermaid
flowchart LR
    DJ([🎧 DJ]) -- "gets the DJ role" --> ONB["Welcome DM<br/>5 questions + logo"]
    ONB --> N[("🗂️ Notion<br/>Events · Slots · DJs")]
    DJ -- "buttons in DMs" --> BOOK["Book / cancel a slot"]
    BOOK --> N
    N -- "open slots" --> INV["Automatic invites<br/>+ day-before reminders"]
    INV --> DJ
    N -- "every change" --> LINE["📋 Lineup Update<br/>#dj-lineup"]
    BOOK --> LOG["🔔 #rave-log<br/>bookings · new DJs · problems"]
    ORG([🛠️ Organizer]) -- "/setup · commands · admin page" --> N
```

1. **A DJ joins.** The DJ role triggers a welcome DM. The DJ answers a few questions and joins the *Active DJ Booking Group*, or books alone.
2. **They book.** A button menu in the DMs shows events and slots. Booking is race-condition safe.
3. **Gaps get filled.** Before each event the bot invites DJs who have not been asked recently and sends reminders the day before.
4. **You stay in control.** The lineup post, the log channel and Notion show the same truth. Organizers can force-book or free any slot with a command.

---

## 📸 Screenshots

<table>
  <tr>
    <td width="50%" valign="top"><b>DJ onboarding in the DMs</b><br><img src="docs/assets/onboarding.png" alt="DJ onboarding conversation in Discord DMs"></td>
    <td width="50%" valign="top"><b>Lineup post with a booking button</b><br><img src="docs/assets/lineup-post.png" alt="Lineup Update post"><br><br><b>Guided setup</b><br><img src="docs/assets/setup-wizard.png" alt="Setup wizard"></td>
  </tr>
  <tr>
    <td width="50%" valign="top"><b>Admin log channel</b><br><img src="docs/assets/admin-log.png" alt="Admin log channel"></td>
    <td width="50%" valign="top"><b>Admin page (space themed, local network)</b><br><img src="docs/assets/admin-page.png" alt="Admin page"></td>
  </tr>
</table>

<sub>The screenshots show the real texts and layouts, rendered from the sources in <code>docs/assets/src</code>.</sub>

---

## 🚀 Quick start

> **TL;DR** Create a Discord bot, a Notion integration and a PostgreSQL 16 database, start the container `ghcr.io/m4xxx/ravebot:latest` with six variables, then run `/setup` in your server.

You need about 20 minutes. The steps below use Unraid; Docker, Docker Compose and Podman work the same way (see the [user guide](docs/user-guide.md#5-deployment-docker-compose)).

### Step 1: Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** → **Reset Token** → copy the token. This is your `DISCORD_BOT_TOKEN`.
3. On the same page, under **Privileged Gateway Intents**, turn on **Server Members Intent**. Without it DJ onboarding never starts.
4. **Installation** (or **OAuth2 → URL Generator**): scopes `bot` and `applications.commands`. Use this invite link and replace `YOUR_APP_ID` with the **Application ID** from **General Information**:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=268520448
   ```

   The permissions are *View Channels, Send Messages, Embed Links, Read Message History* and *Manage Roles*.
5. Open the link and add the bot to your server.
6. In Discord, turn on **Settings → Advanced → Developer Mode**, right-click your name → **Copy User ID**. This is your `ADMIN_USER_IDS`.

### Step 2: Create the Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration** (type **Internal**, capabilities *Read*, *Insert* and *Update content*). Copy the secret. This is your `NOTION_API_KEY`.
2. Create one **empty page** (for example "RaveBot"), open `...` → **Connections** and add your integration.
3. Copy the page link. The 32 characters at the end of the URL are your `NOTION_PARENT_PAGE_ID`.

The bot creates the Events, Slots and DJs databases under that page by itself.

### Step 3: Prepare the database

Install a **PostgreSQL 16** container (on Unraid: **Apps** → search *PostgreSQL 16*) and create a database and a user, for example `ravebot` / `ravebot`. Your connection string looks like this, with the IP of your Unraid server (not `localhost`):

```
postgresql://ravebot:YOUR_PASSWORD@192.168.1.100:5432/ravebot
```

### Step 4: Add the RaveBot container

**Unraid, with the ready-made template (easiest)**

1. Open a terminal on Unraid (or use the web terminal) and run:

   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/ravebot.xml \
     https://raw.githubusercontent.com/M4XXX/ravebot/main/unraid/ravebot.xml
   ```
2. **Docker** → **Add Container** → **Template** → pick **RaveBot** under *User templates*.
3. Fill in the variables from the table below and click **Apply**.

**Unraid, by hand**

1. **Docker** → **Add Container**.
2. **Name** `ravebot`, **Repository** `ghcr.io/m4xxx/ravebot:latest`, **Network Type** `bridge`.
3. **Add another Path, Port, Variable…** → **Variable** for each row of the table below.
4. Optional, for the admin page: add a **Port** (container port `8080`, any free host port) and the variable `ADMIN_PAGE_PASSWORD`.
5. Click **Apply**.

**Anywhere else (plain Docker)**

```bash
docker run -d --name ravebot --restart unless-stopped \
  -e DISCORD_BOT_TOKEN=... \
  -e POSTGRES_URL=postgresql://ravebot:PASSWORD@HOST:5432/ravebot \
  -e NOTION_API_KEY=... \
  -e NOTION_PARENT_PAGE_ID=... \
  -e ADMIN_USER_IDS=123456789012345678 \
  ghcr.io/m4xxx/ravebot:latest
```

| Variable | Required | What to put in |
|---|:---:|---|
| `DISCORD_BOT_TOKEN` | ✅ | Bot token from step 1 |
| `POSTGRES_URL` | ✅ | Connection string from step 3 |
| `NOTION_API_KEY` | ✅ | Integration secret from step 2 |
| `NOTION_PARENT_PAGE_ID` | ✅ | Page ID from step 2 |
| `ADMIN_USER_IDS` | ✅ | Your Discord user ID (several: separated by commas) |
| `ADMIN_PAGE_PASSWORD` | – | Turns the [admin page](#-admin-page) on. A long random password |
| `ADMIN_PAGE_PORT` | – | Port of the admin page inside the container, default `8080` |
| `LOG_LEVEL` | – | `INFO` (default), `DEBUG`, `WARNING`, `ERROR` |

> The package `ghcr.io/m4xxx/ravebot` must be **public** for Unraid to pull it without a login. The repository owner sets this once under *GitHub → Packages → ravebot → Package settings → Change visibility*.

### Step 5: Check that it runs

Open the container log (**Docker** → container icon → **Logs**). A healthy start ends with:

```
Migrations complete.
asyncpg pool opened successfully.
Bot ready
```

If the bot exits instead, the log says why (a missing variable, an unreachable database or a Notion page that is not shared with the integration). See [Troubleshooting](docs/user-guide.md#8-troubleshooting).

### Step 6: Set it up in Discord

1. In your server run **`/setup`** in a channel only you can see.
2. Follow the six steps: DJ channel, log channel, roles, event defaults, time zone and automatic invites.
3. Press **Activate**. The bot creates your first events in Notion and posts the first Lineup Update.

Until you press *Activate* the bot stays idle on purpose: no posts, no invites, no events. Give someone the DJ role to try the onboarding.

---

## 🔄 Updating

Every release publishes a new image to GitHub Container Registry and a GitHub release with the changes.

| You want | Set the repository to | Update by |
|---|---|---|
| Always the newest release | `ghcr.io/m4xxx/ravebot:latest` | Unraid **Docker** tab → **Check for Updates** → **apply update** |
| A fixed version | `ghcr.io/m4xxx/ravebot:1.1.0` | Change the version number in the container and **Apply** |

The database updates itself: migrations run at every start. Read the [release notes](https://github.com/M4XXX/ravebot/releases) before you update.

**Making a release (maintainers):** `scripts/release.sh 1.2.0` bumps the version, closes the changelog section, commits and tags. Then `git push origin main v1.2.0` starts the build. Details are in the [developer guide](docs/developer-guide.md#7-automated-releases).

---

## 🪐 Admin page

Set `ADMIN_PAGE_PASSWORD` and open `http://YOUR-SERVER:PORT/`. Add admins by Discord user ID, remove them again, see who can use the commands. Admins added there can use every admin command. The page has **no HTTPS**: use it on your local network only and never expose it to the internet. Details: [user guide](docs/user-guide.md#77-the-admin-page).

---

## ⌨️ Commands

All admin commands work on the server only (not in DMs).

| Command | Who | What it does |
|---|---|---|
| `/setup` | Admin | Guided setup; run it again to change settings |
| `/config show` | Admin | Show all settings and the bot status |
| `/create event` | Admin | Create an event in Notion for a date |
| `/post lineup update` | Admin | Post the lineup of the next (or a chosen future) event |
| `/book dj slot` | Admin | Book a slot for any DJ, also a guest |
| `/cancel dj slot` | Admin | Free any booking |
| `/dj add`, `/dj edit`, `/dj remove` | Admin | Add, change or delete a DJ, also guests without Discord |
| `/list all djs` | Admin | List all DJs and who gets automatic invites |
| `/automatic dj booking` | Admin | Put a DJ into or out of the automatic invites |

DJs do not use commands: they talk to the bot in the DMs and press buttons.

---

## 📚 Documentation

- [User guide](docs/user-guide.md): prerequisites, Notion setup, deployment (Unraid, Compose, Podman), setup walkthrough, troubleshooting
- [Developer guide](docs/developer-guide.md): local setup, tests, project structure, migrations, releases
- [Changelog](CHANGELOG.md)

## 🧱 Built with

[disnake](https://docs.disnake.dev/) · [aiohttp](https://docs.aiohttp.org/) · [asyncpg](https://magicstack.github.io/asyncpg/) · [httpx](https://www.python-httpx.org/) · [Pillow](https://python-pillow.org/) · [Notion API](https://developers.notion.com/) · PostgreSQL 16 · Docker · [uv](https://docs.astral.sh/uv/)

## 📄 License

Copyright © 2026 M4XXX.

RaveBot is licensed under the [GNU AGPL-3.0](LICENSE):

- **Free to use and change**, including commercially, as long as you follow the AGPL.
- **Share changes:** if you distribute a changed version, or run one as a service others use over a network (for example a Discord bot), you must offer your users the full source under the AGPL.
- **Credit:** keep the copyright and license notices.

This is a summary; the [LICENSE](LICENSE) text is what applies. To use RaveBot without these obligations, for example in a closed-source product, ask for a paid commercial license: see [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md). Third-party dependencies keep their own licenses.
