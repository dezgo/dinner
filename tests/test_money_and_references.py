import pytest

from app.services.billing import compute_bill
from app.services.money import parse_price, split_cents
from app.services.references import make_reference, name_token, reference_found


# ------------------------------------------------------------------ rounding
@pytest.mark.parametrize("total", [1000, 1001, 1, 0, 9999, 18810, -1710, -1])
@pytest.mark.parametrize("n", [1, 2, 3, 7])
def test_split_always_sums_exactly(total, n):
    parts = split_cents(total, [(f"p{i}", 1) for i in range(n)])
    assert sum(parts.values()) == total
    assert max(parts.values()) - min(parts.values()) <= 1


def test_split_is_deterministic_and_leftover_goes_in_join_order():
    # $10.00 three ways: 334, 333, 333 — the first joiner gets the spare cent.
    assert split_cents(1000, [("a", 1), ("b", 1), ("c", 1)]) == {"a": 334, "b": 333, "c": 333}
    assert split_cents(1000, [("c", 1), ("a", 1), ("b", 1)]) == {"c": 334, "a": 333, "b": 333}


def test_unequal_weights():
    assert split_cents(1200, [("a", 2), ("b", 1)]) == {"a": 800, "b": 400}
    parts = split_cents(1001, [("a", 2), ("b", 1)])
    assert sum(parts.values()) == 1001 and parts["a"] == 667


def test_split_rejects_nonsense():
    with pytest.raises(ValueError):
        split_cents(100, [])
    with pytest.raises(ValueError):
        split_cents(100, [("a", 1), ("a", 1)])


def test_parse_price():
    assert parse_price("12.5") == 1250
    assert parse_price("$12.50") == 1250
    assert parse_price("12") == 1200
    assert parse_price("MP") is None
    assert parse_price("1,200.00") == 120000


def test_bill_shares_sum_to_total_with_proportional_surcharge():
    from app.models import Allocation, OrderLine, Participant

    ppl = [Participant(id=x, dinner_id="d", display_name=x, reference=f"R-{x}") for x in "abc"]
    lines = [
        OrderLine(id="l1", dinner_id="d", name="A", unit_price_cents=1333),
        OrderLine(id="l2", dinner_id="d", name="B", unit_price_cents=2001),
        OrderLine(id="l3", dinner_id="d", name="Shared", unit_price_cents=1000, split_mode="equal"),
        OrderLine(
            id="s", dinner_id="d", name="Surcharge", kind="surcharge", unit_price_cents=433, split_mode="proportional"
        ),
    ]
    allocs = [
        Allocation(line_id="l1", participant_id="a"),
        Allocation(line_id="l2", participant_id="b"),
        *[Allocation(line_id="l3", participant_id=x) for x in "abc"],
    ]
    bill = compute_bill(ppl, lines, allocs, bill_total=1333 + 2001 + 1000 + 433)
    assert sum(p["total"] for p in bill.people.values()) == bill.bill_total
    assert bill.accounted_for
    # c had only a third of the shared dish, so pays the smallest part of the surcharge
    assert bill.lines["s"].shares["c"] < bill.lines["s"].shares["a"]


# ---------------------------------------------------------------- references
def test_references_from_brief():
    taken: set[str] = set()
    r1 = make_reference("DIN7", "Helen", taken)
    taken.add(r1)
    r2 = make_reference("DIN7", "Helen", taken)
    taken.add(r2)
    r3 = make_reference("DIN7", "Helen G", taken)
    assert (r1, r2, r3) == ("DIN7-HELEN", "DIN7-HELEN2", "DIN7-HELENG")


def test_reference_normalisation_and_length():
    assert name_token("Zoë O'Brien-Smith") == "ZOEOBRIENSMITH"
    assert name_token("李") == "GUEST"
    ref = make_reference("DIN123", "Bartholomew-Christopher", set())
    assert len(ref) <= 18 and ref.startswith("DIN123-")
    assert ref.replace("-", "").isalnum() and ref == ref.upper()


def test_reference_collisions_when_truncated():
    taken = set()
    for _ in range(12):
        r = make_reference("DIN99", "Christopherson", taken)
        assert r not in taken and len(r) <= 18
        taken.add(r)


def test_reference_found_exact_loose_and_absent():
    assert reference_found("DIN7-HELEN", "din7 helen thanks") == "exact"
    assert reference_found("DIN7-HELEN", "DIN7HELEN") == "exact"
    # Run together it's contained in the longer reference; the matcher resolves this.
    assert reference_found("DIN7-HELEN", "DIN7-HELEN2") == "loose"
    assert reference_found("DIN7-HELEN2", "DIN7-HELEN2") == "exact"
    assert reference_found("DIN7-HELENG", "DIN7-HELEN G") == "loose"
    assert reference_found("DIN7-HELEN", "dinner!") is None
