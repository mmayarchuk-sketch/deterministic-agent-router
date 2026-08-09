#!/usr/bin/env python3
"""Command-line demonstration.

    python demo.py                              # run the four scripted cases
    python demo.py "my chain skips under load"  # route a question of your own

No dependencies, no network, no API key. Everything below runs from the
standard library, which means a reviewer can see the behaviour in five seconds
rather than taking it on trust.
"""

from __future__ import annotations

import sys

from compose import compose
from gates import check_gate
from router import load_registry, route

REGISTRY = load_registry()


def run(question: str, facts: dict[str, object], *, title: str = "") -> None:
    if title:
        print("=" * 72)
        print(title)
        print("=" * 72)

    matches = route(question, REGISTRY)

    print(f"Q: {question}")
    print()
    print("ROUTE")
    if matches:
        for match in matches:
            print(f"  {match.explain()}")
    else:
        print("  (no domain matched)")
    print()

    if not matches:
        # Nothing was routed, so there is nothing to gate. Say so and stop.
        print(compose(question, matches, REGISTRY).render())
        print()
        return

    gate = check_gate(matches, REGISTRY, facts)
    print(gate.report())
    print()

    if not gate.passed:
        print()
        return

    print(compose(question, matches, REGISTRY).render())
    print()


CASES = [
    (
        "Case 1 — a clean route with all inputs present",
        "My chain skips under load but shifting is fine when I am freewheeling.",
        {"drivetrain_speeds": 11},
    ),
    (
        "Case 2 — the gate halts and names what is missing",
        "What tyre pressure should I run?",
        {"tyre_width_mm": 32},  # rider_weight_kg deliberately absent
    ),
    (
        "Case 3 — two disciplines, each signed separately",
        "The brake lever feels spongy and the rear wheel has a loose spoke.",
        {"brake_type": "hydraulic disc", "rider_weight_kg": 78, "tyre_width_mm": 32},
    ),
    (
        "Case 4 — out of scope, and the system says so",
        "Which route should I ride through the Carpathians in October?",
        {},
    ),
]


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        run(" ".join(argv[1:]), {})
        return 0

    print(f"Registry: {REGISTRY.subject}, version {REGISTRY.version}, "
          f"{len(REGISTRY.domains)} domains\n")
    for title, question, facts in CASES:
        run(question, facts, title=title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
