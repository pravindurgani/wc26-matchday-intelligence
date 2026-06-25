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
