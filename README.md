# deterministic-agent-router

A small, dependency-free reference implementation of a pattern for multi-agent
knowledge systems: **route deterministically, attribute every block, grade every
claim, and stop at a gate rather than invent a number.**

```bash
python demo.py                                  # four scripted cases
python demo.py "my chain skips under load"      # route your own question
python -m unittest discover -s tests -v         # 15 tests, no network
```

Python 3.10+, standard library only. Nothing to install, no API key.

---

## Why this exists

Most agent systems choose their route with the model itself, or with a
similarity search over embeddings. Both work, and both give up three things I
am not willing to give up in advisory work:

- **Replay.** The same question tomorrow should take the same path it took today.
- **Explanation.** "Why did it answer that?" should be answerable by pointing at
  a line in a file, not by re-reading a chain of thought.
- **Argument.** A domain expert should be able to look at the routing rules and
  say "that is wrong, move it" — which requires the rules to be somewhere they
  can look.

So routing here is a pure function of `(query, registry)`. No model call, no
randomness, no dependence on dictionary ordering. `tests/test_router.py` asserts
this rather than the README claiming it: the same query is routed 500 times and
must produce an identical result each time.

The cost is real and worth naming. A registry has to be written and maintained
by hand, it will not generalise to a question nobody anticipated, and it needs
an out-of-scope path that gets used often enough to be annoying. That trade is
correct when an answer has to survive review by a third party — a bank, an
insurer, a regulator — and wrong when you are building open-ended chat.

## What it demonstrates

**1. Deterministic routing over a registry.** Domains declare the terms that
belong to them. Matching is whole-word, so `gear` does not fire on `gearbox`.
Ties break alphabetically — arbitrary, but *fixed*, which is what makes the
function replayable.

**2. Attribution per block.** Every section of the answer carries the name of
the domain that produced it. A block with no attribution is a defect, not a
style choice.

**3. Evidence grading.** Every claim is `SUPPORTED`, `MIXED` or `FOLKLORE`, and
is rendered with that grade attached. A well-evidenced finding and a piece of
workshop lore must not read identically, however fluent the prose. Three levels,
deliberately: finer-grained confidence scores invite false precision.

**4. Acceptance gates.** Each domain declares the inputs it needs. If they are
absent the run **halts and names what is missing** instead of producing a
plausible figure. This is the case that matters most — a confident wrong number
looks exactly like a good answer, which is what makes it expensive.

```
Q: What tyre pressure should I run?

ROUTE
  wheels_tyres (score 2) matched on: pressure, tyre

GATE HALTED — the run cannot continue. Missing values:
  - rider_weight_kg  (required by: wheels_tyres)

Supply these and re-run. No figure is produced without them.
```

**5. An explicit out-of-scope path.** When nothing matches, the system says so
and flags the question for external sourcing. A system that always finds
something to say will eventually say something it cannot support.

## Layout

| File | What it holds |
|---|---|
| `registry.json` | The domains: match terms, required inputs, graded claims |
| `router.py` | Normalisation, whole-word matching, deterministic selection |
| `gates.py` | Required-input checks; halts and names what is missing |
| `compose.py` | Assembly with attribution and confidence rendering |
| `demo.py` | CLI: four scripted cases, or route your own question |
| `tests/` | Determinism, scope, gates, attribution, registry integrity |

## Scope and honesty

This is a reference implementation of a **pattern**, sized to be read in one
sitting. It is not a framework and does not want to be one. There is no LLM in
the loop at all — the composition step here just renders registry content,
because the point being demonstrated is the machinery around generation, not
generation itself. In a real system the block content comes from a model; the
routing, the gates, the attribution and the grading stay exactly as they are.

The bicycle domain is a deliberately neutral toy. It carries no code, data or
domain content from any employer or client — everything here was written from
scratch for this repository.

## Licence

MIT — see `LICENSE`.
