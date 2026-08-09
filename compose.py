"""Answer assembly with attribution and evidence grading.

Two rules are enforced here rather than left to the writer:

1. Every block carries the name of the domain that produced it. A block with
   no attribution is a defect, not a style choice.
2. Every claim carries a confidence grade. A well-supported finding and a piece
   of workshop folklore must not read identically, however fluent the prose.

The grades are deliberately coarse. Fine-grained confidence scores invite
false precision; three levels can be defended.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from router import Match, Registry

__all__ = ["CONFIDENCE_ORDER", "Block", "Answer", "compose"]


# Strongest first. Anything outside this set is rejected at load time by the
# integrity test, so a typo in the registry cannot silently downgrade a claim.
CONFIDENCE_ORDER = ("SUPPORTED", "MIXED", "FOLKLORE")

_LABEL = {
    "SUPPORTED": "supported by published sources",
    "MIXED": "contested — evidence points both ways",
    "FOLKLORE": "traditional practice, not established",
}


@dataclass(frozen=True)
class Line:
    statement: str
    confidence: str
    source: str


@dataclass(frozen=True)
class Block:
    domain_id: str
    owner: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Answer:
    question: str
    blocks: tuple[Block, ...]
    unrouted_note: str | None = None

    def render(self) -> str:
        out = [f"Q: {self.question}", ""]
        if self.unrouted_note:
            out.append(self.unrouted_note)
            return "\n".join(out)

        for block in self.blocks:
            out.append(f"— {block.owner} [{block.domain_id}]")
            for line in block.lines:
                out.append(f"  {line.statement}")
                out.append(f"    confidence: {line.confidence} ({_LABEL[line.confidence]})")
                out.append(f"    source: {line.source}")
            out.append("")

        signatures = ", ".join(block.owner for block in self.blocks)
        out.append(f"Answered by: {signatures}")
        return "\n".join(out)


def compose(
    question: str,
    matches: Sequence[Match],
    registry: Registry,
) -> Answer:
    if not matches:
        note = (
            "OUT OF SCOPE — no discipline in this registry covers the question.\n"
            "Flagged for external sourcing rather than answered. An answer would "
            "have to come from outside the system, with a source and a date."
        )
        return Answer(question=question, blocks=(), unrouted_note=note)

    blocks = []
    for match in matches:
        domain = registry.by_id(match.domain_id)
        lines = tuple(
            Line(
                statement=claim.statement,
                confidence=claim.confidence,
                source=claim.source,
            )
            for claim in sorted(domain.claims, key=lambda c: (CONFIDENCE_ORDER.index(c.confidence), c.topic))
        )
        blocks.append(Block(domain_id=domain.id, owner=domain.owner, lines=lines))

    return Answer(question=question, blocks=tuple(blocks))
