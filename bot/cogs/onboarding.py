"""Onboarding cog — implemented in Story 2.x.

Handles: FR1–8 — DJ role detection, DM onboarding flow, modal, Active Booking role.
"""
from disnake.ext import commands

from bot.context import BotContext


class OnboardingCog(commands.Cog):
    """Handles DJ role detection and DM onboarding flow."""

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx
