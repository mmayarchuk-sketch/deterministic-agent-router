"""Acceptance gates.

A gate is a checkpoint between stages that asks one question: are the values
this stage needs actually present? If they are not, the run stops and names
what is missing.

The alternative — carrying on and producing a plausible number — is the single
most expensive failure mode in an advisory system, because the output looks
exactly like a good answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from router import Match, Registry

__all__ = ["GateResult", "check_gate"]


@dataclass(frozen=True)
class GateResult:
    passed: bool
    missing: tuple[tuple[str, str], ...] = ()  # (domain_id, required_key)

    def report(self) -> str:
        if self.passed:
            return "GATE PASSED — all required values present."
        lines = ["GATE HALTED — the run cannot continue. Missing values:"]
        for domain_id, key in self.missing:
            lines.append(f"  - {key}  (required by: {domain_id})")
        lines.append("")
        lines.append("Supply these and re-run. No figure is produced without them.")
        return "\n".join(lines)


def check_gate(
    matches: tuple[Match, ...],
    registry: Registry,
    facts: Mapping[str, object],
) -> GateResult:
    """Verify that every routed domain has the inputs it declared it needs."""
    missing: list[tuple[str, str]] = []

    for match in matches:
        domain = registry.by_id(match.domain_id)
        for key in domain.requires:
            value = facts.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append((domain.id, key))

    # Deterministic ordering, so two runs report the same list in the same order.
    missing.sort()
    return GateResult(passed=not missing, missing=tuple(missing))
