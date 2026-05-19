"""Booking cog — implemented in Story 4.x.

Handles: FR14–24 — self-service booking and cancellation Views and handlers.
"""
from disnake.ext import commands

from bot.context import BotContext


class BookingCog(commands.Cog):
    """Handles self-service slot booking and cancellation."""

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx
