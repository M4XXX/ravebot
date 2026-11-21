from bot.db.mirror import MirrorRow
from bot.sync.payloads import payload_hash
from bot.sync.plan import Side, plan


def dj(name, did=1):
    return {"discord_id": did, "dj_name": name, "vrchat_name": None, "link": None, "alt_link": None}


def mrow(key, payload, page="pg1"):
    return MirrorRow("dj", key, page, payload_hash(payload) if payload else None, payload, None)


def run(pg, notion, mirror, open_keys=frozenset(), unmatched=(), pages=None):
    pages = pages if pages is not None else {k: f"page-{k}" for k in notion}
    return plan(
        "dj", pg, Side(notion, pages), mirror, set(open_keys), unmatched_notion=list(unmatched)
    )


def kinds(changes):
    return [c.kind for c in changes]


BASE = dj("A")
MIR = {"1": mrow("1", BASE)}


def test_unchanged_is_empty():
    assert run({"1": BASE}, {"1": BASE}, MIR) == []


def test_pg_changed_pushes():
    c = run({"1": dj("B")}, {"1": BASE}, MIR)
    assert kinds(c) == ["push"] and c[0].key == "1"


def test_notion_changed_applies():
    c = run({"1": BASE}, {"1": dj("N")}, MIR)
    assert kinds(c) == ["apply"] and c[0].notion["dj_name"] == "N" and c[0].page_id == "page-1"


def test_both_changed_conflicts():
    assert kinds(run({"1": dj("B")}, {"1": dj("N")}, MIR)) == ["conflict"]


def test_both_changed_same_agrees():
    assert kinds(run({"1": dj("B")}, {"1": dj("B")}, MIR)) == ["agree"]


def test_open_key_is_skipped():
    assert run({"1": dj("B")}, {"1": dj("N")}, MIR, open_keys={"1"}) == []


def test_page_gone_is_deleted_in_notion():
    pg = {str(i): dj("A", i) for i in (1, 2)}
    mirror = {k: mrow(k, v) for k, v in pg.items()}
    c = run(pg, {"2": pg["2"]}, mirror)
    assert kinds(c) == ["deleted_in_notion"] and c[0].key == "1" and c[0].page_id == "pg1"


def test_key_changed_on_matched_page():
    c = run({"1": BASE}, {"1": dj("A", 99)}, MIR)
    assert kinds(c) == ["key_changed"]


def test_slot_key_changed():
    old = {"event_uid": "e", "number": 1, "discord_id": None}
    m = {"e:0": MirrorRow("slot", "e:0", "p", payload_hash(old), old, None)}
    new = {**old, "number": 2}
    c = plan("slot", {"e:0": old}, Side({"e:0": new}, {"e:0": "p"}), m, set(), unmatched_notion=[])
    assert kinds(c) == ["key_changed"]


def test_new_in_notion():
    c = run({"1": BASE}, {"1": BASE}, MIR, unmatched=[{**dj("N", 7), "_page_id": "px"}])
    assert kinds(c) == ["new_in_notion"] and c[0].page_id == "px" and "_page_id" not in c[0].notion


def test_unmatched_with_duplicate_key_is_key_changed():
    c = run({"1": BASE}, {"1": BASE}, MIR, unmatched=[{**dj("N", 1), "_page_id": "px"}])
    assert kinds(c) == ["key_changed"]


def test_pg_without_mirror_pushes():
    c = run({"1": BASE, "2": dj("B", 2)}, {"1": BASE}, MIR)
    assert kinds(c) == ["push"] and c[0].key == "2"


def test_pg_deleted_but_notion_still_has_it_pushes():
    c = run({"2": dj("B", 2)}, {"1": BASE, "2": dj("B", 2)}, {**MIR, "2": mrow("2", dj("B", 2))})
    assert kinds(c) == ["push"] and c[0].key == "1" and c[0].pg is None


def test_orphan_event_page_is_adopted():
    ev = {"event_id": "Party", "date": "2026-01-01"}
    c = plan("event", {"u1": ev}, Side({}, {}), {}, set(),
             unmatched_notion=[{**ev, "_page_id": "pz", "_bot_id": "u1"}])
    assert [(x.kind, x.key, x.page_id) for x in c] == [("agree", "u1", "pz")]


def test_mass_deletion_guard():
    pg = {str(i): dj("A", i) for i in range(10)}
    mirror = {k: mrow(k, v) for k, v in pg.items()}
    c = run(pg, {"9": pg["9"]}, mirror)
    assert kinds(c) == ["mass_change"]


def test_empty_notion_read_is_mass_change():
    assert kinds(run({"1": BASE}, {}, MIR)) == ["mass_change"]


def test_few_deletions_pass_guard():
    pg = {str(i): dj("A", i) for i in range(10)}
    mirror = {k: mrow(k, v) for k, v in pg.items()}
    c = run(pg, {k: v for k, v in pg.items() if int(k) > 2}, mirror)
    assert kinds(c) == ["deleted_in_notion"] * 3


def test_adoption_skipped_when_mirror_has_page_id():
    ev = {"event_id": "Party", "date": "2026-01-01"}
    m = {"u1": MirrorRow("event", "u1", "old", payload_hash(ev), ev, None)}
    other = {"event_id": "Other", "date": "2026-02-02"}
    c = plan("event", {"u1": ev}, Side({"u1": ev}, {"u1": "old"}), m, set(),
             unmatched_notion=[{**other, "_page_id": "pz", "_bot_id": "u1"}])
    assert kinds(c) == ["new_in_notion"]


def test_empty_notion_without_mirror_page_ids_pushes_everything():
    c = run({"1": BASE, "2": dj("B", 2)}, {}, {})
    assert kinds(c) == ["push", "push"]


def test_empty_notion_with_mirror_page_ids_is_mass_change():
    assert kinds(run({"1": BASE}, {}, MIR)) == ["mass_change"]


def test_event_new_in_notion():
    ev = {"event_id": "Party", "date": "2026-01-01"}
    c = plan("event", {}, Side({}, {}), {}, set(), unmatched_notion=[{**ev, "_page_id": "p"}])
    assert kinds(c) == ["new_in_notion"]


def test_slot_new_in_notion_and_key_changed():
    s = {"event_uid": "e", "number": 1, "discord_id": None}
    c = plan("slot", {}, Side({}, {}), {}, set(), unmatched_notion=[{**s, "_page_id": "p"}])
    assert kinds(c) == ["new_in_notion"]
    c = plan("slot", {"e:0": s}, Side({"e:0": s}, {"e:0": "q"}),
             {"e:0": MirrorRow("slot", "e:0", "q", payload_hash(s), s, None)}, set(),
             unmatched_notion=[{**s, "discord_id": 5, "_page_id": "p"}])
    assert kinds(c) == ["key_changed"]
