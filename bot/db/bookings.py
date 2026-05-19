"""Slot booking repository — implemented in Story 4.2.

Handles: FR17, FR19, FR23 — slot booking writes with SELECT...FOR UPDATE.
CRITICAL: All slot writes use pessimistic row-level locking inside a PostgreSQL transaction.
"""
