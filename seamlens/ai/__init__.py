"""Layer 4 -- OPTIONAL AI assist for seamlens.

This package is strictly additive and strictly optional. The seamlens core
(extractors / graph / linters / cli core commands) never imports anything from
here, and works with zero AI configured. The portability invariant is unchanged:
the only host-aware surface is `seamlens.yaml` (now with an optional `ai:` block
whose credentials live in *environment variables*, never in the file or the core).

What the AI layer consumes is exactly the generic artifacts the core already
produces -- the system graph and the linter findings. It adds three capabilities:

  * atlas   -- narrate the architecture graph in natural language (the DOT itself
               is deterministic and needs no AI).
  * triage  -- rank each finding real vs false-positive with a reason (sharpens
               the discriminator toward zero false positives).
  * evolve  -- the GAN loop: propose a patch for a true-positive finding, then let
               the DETERMINISTIC referee (re-scan the graph) decide if it is
               accepted. The graph is ground truth, so the generator cannot
               reward-hack the discriminator.
"""
