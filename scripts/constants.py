"""Single source of truth for simulator scoring constants.

These three constants are mirrored in the .gs betting engine
(``wc26-engine-gs/WC26_Engine_AppsScript_v*.gs`` as ``GOAL_GRID_TAU`` /
``GOAL_GRID_MAX_GOALS``) and cross-checked at runtime by
``scripts/live/verify_goal_grid_agreement.py``.

Edit values here once — do not shadow-declare elsewhere. The
``tests/live/test_r13_misc.py`` R13 C1/C2 invariants enforce that
consumer files import these symbols from this module rather than
re-declaring them, removing the drift surface that the verify script
was added to defend.
"""
from __future__ import annotations

MAX_G: int = 15
"""Dixon-Coles score-matrix upper bound (also `build_score_matrix`
``max_g`` default in ``scripts/03_simulate.py``)."""

DC_RHO: float = -0.13
"""Dixon-Coles low-score correlation tau (``dc_rho`` in
``scripts/03_simulate.py`` ``DEFAULTS``)."""

NB_ALPHA: float = 5.0
"""Negative-binomial dispersion (``nb_dispersion`` in
``scripts/03_simulate.py`` ``DEFAULTS``)."""

PEN_ELO_SLOPE: float = 600.0
"""Penalty-shootout Elo logistic slope (``pen_elo_slope`` in
``scripts/03_simulate.py`` ``DEFAULTS``, which imports this symbol):

    P(home wins shootout) = 1 / (1 + 10 ** ((elo_away - elo_home) / SLOPE))

Consumed by ``resolve_knockout`` in the Monte Carlo sim and by the
closed-form draw-split in ``scripts/live/export_ko_advance.py`` (R17 P2:
the published ``p_advance_match`` must price the draw mass with the SAME
tie-break model the sim actually plays, not a 50/50 prior)."""

ET_LAMBDA_DIVISOR: float = 3.0
"""Extra time is 30 minutes = 1/3 of regulation, so knockout ET goals are
sampled at Poisson(λ / ET_LAMBDA_DIVISOR) per side (``resolve_knockout``
in ``scripts/03_simulate.py``, which imports this symbol). Reused by the
closed-form ET goal matrix in ``scripts/live/export_ko_advance.py``.
Kept as 3.0 (not 1/3 factor) so ``lam / ET_LAMBDA_DIVISOR`` is bit-
identical to the sim's historical ``lam / 3``."""
