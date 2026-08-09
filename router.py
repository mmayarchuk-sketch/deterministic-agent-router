"""Deterministic routing over a registry of named domains.

The routing decision is a pure function of (query, registry). There is no model
call, no randomness, and no dependence on dictionary insertion order: the same
question always takes the same route, and the route can always be explained by
pointing at a line in the registry.

That property is the whole point. A retrieval-ranked or model-chosen route is
convenient, but it cannot be replayed, audited or argued with — and an advisory
answer that has to survive review by a third party needs all three.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = ["Domain", "Registry", "Match", "normalise", "load_registry", "route"]


# --------------------------------------------------------------------------- #
# Registry model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Claim:
    topic: str
    statement: str
    confidence: str
    source: str


@dataclass(frozen=True)
class Domain:
    id: str
    owner: str
    match: tuple[str, ...]
    requires: tuple[str, ...] = ()
    claims: tuple[Claim, ...] = ()


@dataclass(frozen=True)
class Registry:
    version: str
    subject: str
    domains: tuple[Domain, ...]

    def by_id(self, domain_id: str) -> Domain:
        for domain in self.domains:
            if domain.id == domain_id:
                return domain
        raise KeyError(domain_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(domain.id for domain in self.domains)


@dataclass(frozen=True)
class Match:
    domain_id: str
    owner: str
    score: int
    terms: tuple[str, ...] = field(default=())

    def explain(self) -> str:
        return f"{self.domain_id} (score {self.score}) matched on: {', '.join(self.terms)}"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_registry(path: str | Path = "registry.json") -> Registry:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    domains = []
    for entry in raw["domains"]:
        claims = tuple(
            Claim(
                topic=claim["topic"],
                statement=claim["statement"],
                confidence=claim["confidence"],
                source=claim["source"],
            )
            for claim in entry.get("claims", [])
        )
        domains.append(
            Domain(
                id=entry["id"],
                owner=entry["owner"],
                # Sorted so that the registry file can be edited in any order
                # without changing behaviour.
                match=tuple(sorted(term.lower() for term in entry["match"])),
                requires=tuple(entry.get("requires", ())),
                claims=claims,
            )
        )
    domains.sort(key=lambda d: d.id)
    return Registry(version=raw["version"], subject=raw["subject"], domains=tuple(domains))


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Fold a query to a stable comparison form.

    Unicode normalisation first, so that visually identical inputs typed on
    different keyboards route identically.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def _term_hits(haystack: str, term: str) -> bool:
    """Whole-word (or whole-phrase) containment, so 'gear' does not match 'gearbox'."""
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack) is not None


def _weight(term: str) -> int:
    """Multi-word terms are more specific, so they count for more."""
    return len(term.split())


def route(
    query: str,
    registry: Registry,
    *,
    max_domains: int = 3,
    min_score: int = 1,
) -> tuple[Match, ...]:
    """Return the matching domains, strongest first.

    Ties are broken by domain id, alphabetically. That is arbitrary but it is
    *fixed*, which is what makes the function replayable.
    """
    haystack = normalise(query)
    matches: list[Match] = []

    for domain in registry.domains:
        hit_terms = tuple(term for term in domain.match if _term_hits(haystack, term))
        if not hit_terms:
            continue
        score = sum(_weight(term) for term in hit_terms)
        if score < min_score:
            continue
        matches.append(
            Match(domain_id=domain.id, owner=domain.owner, score=score, terms=hit_terms)
        )

    matches.sort(key=lambda m: (-m.score, m.domain_id))
    return tuple(matches[:max_domains])


def unrouted(query: str, registry: Registry) -> bool:
    """True when no discipline in the registry covers the question.

    Saying so is a feature. A system that always finds something to say will
    eventually say something it cannot support.
    """
    return not route(query, registry)


def selected_ids(matches: Iterable[Match]) -> tuple[str, ...]:
    return tuple(match.domain_id for match in matches)
