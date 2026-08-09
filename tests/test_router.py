"""Tests for the properties the pattern actually claims.

Determinism is a claim, so it is tested rather than asserted in the README.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compose import CONFIDENCE_ORDER, compose  # noqa: E402
from gates import check_gate  # noqa: E402
from router import load_registry, normalise, route, selected_ids  # noqa: E402

REGISTRY = load_registry(Path(__file__).resolve().parents[1] / "registry.json")


class TestDeterminism(unittest.TestCase):
    def test_same_query_same_route_many_times(self):
        query = "my chain skips under load and the cassette looks worn"
        first = selected_ids(route(query, REGISTRY))
        for _ in range(500):
            self.assertEqual(selected_ids(route(query, REGISTRY)), first)

    def test_registry_file_order_does_not_matter(self):
        # load_registry sorts domains and match terms, so an edit that only
        # reorders the file cannot change behaviour.
        ids = REGISTRY.ids()
        self.assertEqual(list(ids), sorted(ids))
        for domain in REGISTRY.domains:
            self.assertEqual(list(domain.match), sorted(domain.match))

    def test_ties_break_alphabetically(self):
        # "tool" and "frame" are single-word terms in different domains.
        matches = route("frame tool", REGISTRY)
        scores = [m.score for m in matches]
        self.assertEqual(scores, sorted(scores, reverse=True))
        tied = [m.domain_id for m in matches if m.score == scores[0]]
        self.assertEqual(tied, sorted(tied))


class TestNormalisation(unittest.TestCase):
    def test_punctuation_and_case_folded(self):
        self.assertEqual(normalise("  My CHAIN, skipping!  "), "my chain skipping")

    def test_whole_word_matching(self):
        # 'gear' must not fire on 'gearbox'
        self.assertEqual(route("gearbox oil", REGISTRY), ())
        self.assertTrue(route("gear indexing", REGISTRY))


class TestScope(unittest.TestCase):
    def test_out_of_scope_returns_nothing(self):
        self.assertEqual(route("what should I have for dinner", REGISTRY), ())

    def test_out_of_scope_answer_declines(self):
        answer = compose("what should I have for dinner", (), REGISTRY)
        self.assertIn("OUT OF SCOPE", answer.render())
        self.assertEqual(answer.blocks, ())


class TestGates(unittest.TestCase):
    def test_gate_halts_and_names_missing_values(self):
        matches = route("what tyre pressure should I run", REGISTRY)
        result = check_gate(matches, REGISTRY, {"tyre_width_mm": 32})
        self.assertFalse(result.passed)
        self.assertIn(("wheels_tyres", "rider_weight_kg"), result.missing)
        self.assertIn("rider_weight_kg", result.report())

    def test_gate_passes_when_complete(self):
        matches = route("what tyre pressure should I run", REGISTRY)
        result = check_gate(
            matches, REGISTRY, {"tyre_width_mm": 32, "rider_weight_kg": 78}
        )
        self.assertTrue(result.passed)

    def test_empty_string_counts_as_missing(self):
        matches = route("chain wear", REGISTRY)
        self.assertFalse(check_gate(matches, REGISTRY, {"drivetrain_speeds": "  "}).passed)


class TestAttributionAndGrading(unittest.TestCase):
    def test_every_block_is_signed(self):
        answer = compose("chain wear", route("chain wear", REGISTRY), REGISTRY)
        self.assertTrue(answer.blocks)
        for block in answer.blocks:
            self.assertTrue(block.owner.strip())
            self.assertIn(block.owner, answer.render())

    def test_every_claim_is_graded_and_sourced(self):
        for domain in REGISTRY.domains:
            for claim in domain.claims:
                self.assertIn(claim.confidence, CONFIDENCE_ORDER, msg=domain.id)
                self.assertTrue(claim.source.strip(), msg=domain.id)

    def test_strongest_evidence_is_presented_first(self):
        answer = compose("chain wear", route("chain wear", REGISTRY), REGISTRY)
        for block in answer.blocks:
            ranks = [CONFIDENCE_ORDER.index(line.confidence) for line in block.lines]
            self.assertEqual(ranks, sorted(ranks))


class TestRegistryIntegrity(unittest.TestCase):
    def test_domain_ids_are_unique(self):
        ids = REGISTRY.ids()
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_domain_has_an_owner_and_terms(self):
        for domain in REGISTRY.domains:
            self.assertTrue(domain.owner.strip(), msg=domain.id)
            self.assertTrue(domain.match, msg=domain.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
