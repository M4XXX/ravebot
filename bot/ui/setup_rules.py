"""Rules step of the setup wizard: draft rule helpers and the "Add rule" popup.

The draft keeps its rules in `data["rules"]` as dicts (`id` is the stored rule id, None
for a rule added in the wizard) and a rule that waits for its start event in
`data["rule_pending"]`. Rule numbers shown in the wizard are positions in that list,
starting at 1. Nothing here touches the database.
"""
import datetime
from collections.abc import Awaitable, Callable
from typing import Any

import disnake

from bot.services.schedule import clean_value
from bot.services.schedule_rules import Field, Rule, conflicts
from bot.ui import texts

MAX_RULES = 25
RULE_FIELDS: tuple[Field, ...] = ("genre", "location")


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "field": rule.field,
        "value": rule.value,
        "every": rule.every,
        "anchor": rule.anchor.isoformat(),
    }


def rule_from_dict(raw: dict[str, Any], rule_id: int | None = None) -> Rule:
    """The rule of a draft dict; `rule_id` replaces the stored id when given."""
    return Rule(
        rule_id if rule_id is not None else raw.get("id"),
        raw["field"],
        raw["value"],
        int(raw["every"]),
        datetime.date.fromisoformat(raw["anchor"]),
    )


def numbered_rules(data: dict[str, Any]) -> list[Rule]:
    """The draft's rules with their position (from 1) as id."""
    return [rule_from_dict(raw, i) for i, raw in enumerate(data.get("rules") or [], 1)]


def stored_rules(data: dict[str, Any]) -> list[Rule]:
    """The draft's rules with their stored ids (None for new ones)."""
    return [rule_from_dict(raw) for raw in data.get("rules") or []]


def conflict_pairs(rules: list[Rule], interval_days: int) -> list[tuple[Rule, Rule]]:
    """Pairs (lower number, higher number) of numbered rules that can hit one date."""
    pairs: list[tuple[Rule, Rule]] = []
    for rule in rules:
        for other in conflicts(rule, rules, interval_days):
            if rule.id is not None and other.id is not None and rule.id < other.id:
                pairs.append((rule, other))
    return pairs


def rule_summary(rule: Rule, anchor_label: str) -> str:
    return texts.rule_summary(rule.field, rule.value, rule.every, anchor_label)


def parse_rule_fields(values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Check the popup's answers: (pending rule, error text)."""
    chosen = values.get("field")
    field = chosen[0] if isinstance(chosen, list | tuple) and chosen else chosen
    if field not in RULE_FIELDS:
        return None, texts.SETUP_RULES_STALE
    try:
        every = int(str(values.get("every") or "").strip())
    except ValueError:
        return None, texts.RULE_EVERY_RANGE
    if not 1 <= every <= 52:
        return None, texts.RULE_EVERY_RANGE
    try:
        value = clean_value(field, str(values.get("value") or ""))
    except ValueError as exc:
        return None, str(exc)
    if value is None:
        return None, texts.RULE_VALUE_EMPTY
    return {"field": field, "value": value, "every": every}, None


class RuleModal(disnake.ui.Modal):
    """Popup for a new rule's field, value and repeat; `submit` gets the checked answers."""

    def __init__(
        self,
        message_id: int,
        submit: Callable[[Any, int, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._message_id = message_id
        self._submit = submit
        super().__init__(
            title=texts.RULE_MODAL_TITLE,
            custom_id=f"setup_rule:{message_id}",
            components=[
                disnake.ui.Label(
                    texts.RULE_FIELD_LABEL,
                    disnake.ui.StringSelect(
                        custom_id="field",
                        options=[
                            disnake.SelectOption(label=texts.field_name(f), value=f)
                            for f in RULE_FIELDS
                        ],
                    ),
                ),
                disnake.ui.Label(
                    texts.RULE_VALUE_LABEL,
                    disnake.ui.TextInput(custom_id="value", max_length=80, required=True),
                ),
                disnake.ui.Label(
                    texts.RULE_EVERY_LABEL,
                    disnake.ui.TextInput(
                        custom_id="every", value="1", max_length=2, required=True
                    ),
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        await self._submit(inter, self._message_id, dict(inter.values))
