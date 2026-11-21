import datetime

from bot.services.schedule_rules import Rule, conflicts, reanchor, resolve, rule_hits

I = 7  # noqa: E741
D = datetime.date(2026, 12, 5)


def r(every, offset_weeks, value="A", field="location", id=None):
    return Rule(id, field, value, every, D + datetime.timedelta(days=7 * offset_weeks))


def test_alternating_example_from_spec():
    a, b = r(2, 0, "A", id=1), r(2, 1, "B", id=2)
    got = [resolve("location", D + datetime.timedelta(weeks=n), [a, b], "Z", I) for n in range(5)]
    assert got == ["A", "B", "A", "B", "A"]


def test_four_rule_rotation_is_conflict_free():
    rules = [r(4, n, f"L{n}", id=n + 1) for n in range(4)]
    for n, rule in enumerate(rules):
        assert conflicts(rule, rules[:n], I) == []


def test_every_2_and_every_3_collide_and_name_the_rule():
    a, b = r(2, 0, id=1), r(3, 1, id=2)
    assert conflicts(b, [a], I) == [a]


def test_same_offset_mod_gcd_conflicts():
    assert conflicts(r(4, 2, id=2), [r(2, 0, id=1)], I) != []
    assert conflicts(r(4, 1, id=2), [r(2, 0, id=1)], I) == []


def test_off_grid_anchors_never_conflict():
    a = r(1, 0, id=1)
    b = Rule(2, "location", "B", 1, D + datetime.timedelta(days=3))
    assert conflicts(b, [a], I) == []
    assert rule_hits(b, D + datetime.timedelta(days=7), I) is False


def test_different_fields_never_conflict():
    assert conflicts(r(1, 0, field="genre", id=2), [r(1, 0, id=1)], I) == []


def test_default_and_none():
    assert resolve("genre", D, [], "Techno", I) == "Techno"
    assert resolve("genre", D, [], None, I) is None
    assert resolve("location", D - datetime.timedelta(days=7), [r(1, 0)], None, I) is None


def test_reanchor():
    assert reanchor(D, D - datetime.timedelta(days=28), 14) == D
    assert reanchor(D + datetime.timedelta(days=1), D, 14) == D + datetime.timedelta(days=14)
