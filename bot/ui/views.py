"""discord.ui.View subclasses — implemented in Story 2.x–5.x.

Views: OutreachView, SlotSelectionView, BookingConfirmView,
       CancellationView, CancelConfirmView, FallbackMenuView.

RULE: Every View subclass MUST:
  1. Accept BotContext as a constructor argument
  2. Implement on_timeout() — re-present flow or send warm timeout message
  3. Acquire user's asyncio.Lock on entry; release on completion, abandonment, and in on_timeout()
"""
