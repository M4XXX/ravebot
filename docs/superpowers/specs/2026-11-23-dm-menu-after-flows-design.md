# DM menu after flows: design

## Intent
A DJ should not have to type a message to get the buttons back. After a booking, cancel, outreach or onboarding flow ends, the bot sends the default menu ("I work with buttons. What do you want to do?") about 2 seconds later, so the menu is the last message in the DM.

## Decisions (from the owner)
- Applies whether the flow ended successfully or not.
- After a lineup status request the bot sends no menu. This is the only exception.
- The bot deletes its own old menu messages in that DM. It tracks the id of the last menu per DJ and deletes it when it sends a new one.
- Delay: about 2 seconds.

## Behaviour
- Trigger A: a user's FlowLock is freed (booking, cancel, outreach accept, never mind, errors, slot taken). Lineup status never takes the lock, so it never triggers.
- Trigger B: a DJ conversation ends: onboarding done, onboarding declined, onboarding or edit cancelled. Overviews, questions, summaries and errors do not trigger.
- An expired, untouched flow (the 15 minute timeout) does not trigger a menu (my call: no unprompted DM).
- `MenuService.schedule(user_id)` is debounced: a new call cancels the pending one. After the delay it skips when the user's flow lock is held, a DJ conversation is open, the bot is idle, or the user id is not positive (guest DJ).
- Sending a menu: delete the stored old menu (ignore NotFound and Forbidden), send the menu with `FallbackMenuView` built from the DJ's current state, store the new message id. The free-text menu in `on_message` uses the same send, so it also deletes the old menu and records the new one, and it cancels a pending scheduled menu.
- Every failure is logged and swallowed; a failed menu never breaks a flow.

## Storage
Table `dm_menus(user_id BIGINT PRIMARY KEY, channel_id BIGINT NOT NULL, message_id BIGINT NOT NULL, updated_at)`, migration 0022.

## Non-goals
No menu after lineup status, overview, or in-flow steps. No change to the menu content.

## Left to the builder
Names of helpers, where the DJ state lookup lives (reuse the `on_message` lookup).
