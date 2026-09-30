"""Integer-cent arithmetic.

Every split uses the largest-remainder method: each person gets the floor of
their exact share, and the leftover cents go one at a time to the largest
fractional parts, ties broken by the order the caller gives (join order).
The pieces therefore always sum to exactly the amount being split, and the
same inputs always produce the same cents.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


def split_cents(total: int, weights: Sequence[tuple[str, int]]) -> dict[str, int]:
    """Split `total` cents across keys in proportion to non-negative weights."""
    keys = [k for k, _ in weights]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate keys in split")
    if any(w < 0 for _, w in weights):
        raise ValueError("negative weight")
    weight_sum = sum(w for _, w in weights)
    if weight_sum == 0:
        raise ValueError("nothing to split across")
    if total == 0:
        return dict.fromkeys(keys, 0)

    sign = -1 if total < 0 else 1
    magnitude = abs(total)
    shares = {k: magnitude * w // weight_sum for k, w in weights}
    leftover = magnitude - sum(shares.values())
    # Largest fractional part first; earlier position wins a tie.
    order = sorted(
        range(len(weights)),
        key=lambda i: (-(magnitude * weights[i][1] % weight_sum), i),
    )
    for i in order[:leftover]:
        shares[weights[i][0]] += 1
    return {k: sign * v for k, v in shares.items()}


def fmt(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


_PRICE = re.compile(r"^\s*\$?\s*(-?)\s*\$?\s*(\d{1,6})(?:\.(\d{1,2}))?\s*$")


def parse_price(text: str | None) -> int | None:
    """'12.5', '$12.50', '12' -> cents. Anything else -> None."""
    if text is None:
        return None
    m = _PRICE.match(str(text).replace(",", ""))
    if not m:
        return None
    neg, dollars, frac = m.groups()
    cents = int(dollars) * 100 + int((frac or "0").ljust(2, "0"))
    return -cents if neg else cents
