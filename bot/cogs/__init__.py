"""Discord cogs — event handlers and slash command entrypoints."""

import disnake

# Slash commands that only work on a server (not in DMs).
SERVER_ONLY = disnake.InteractionContextTypes(guild=True)
