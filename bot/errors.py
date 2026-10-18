"""Exception groups for handlers that answer a user after an expected failure.

Catch these, not Exception, where a database or Notion call may fail for reasons outside
the bot (connection lost, timeout, Notion down). Anything else is a bug: let it reach the
view's on_error or the cog's cog_slash_command_error, which log it with a traceback,
answer the user and release the flow lock.

[Source: epics.md#Story 8.6]
"""
import asyncpg
import httpx

# asyncpg: server errors, a closed connection or pool, the network, and pool timeouts
DB_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
)

# Notion reads (poll_events and friends): HTTP status and transport errors, non-JSON bodies
NOTION_READ_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, ValueError)
