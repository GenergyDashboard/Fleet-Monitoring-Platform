"""
Energy powerflow split (dual-anchor reconciliation)
===================================================
Given the four metered energy values for a period - import (grid), export
(to grid), charge (into battery), discharge (from battery) - plus PV
generation and site consumption (load), this module reconstructs the seven
underlying flows:

    PV    -> Load, Batt, Grid
    Batt  -> Load, Grid
    Grid  -> Load, Batt

Source: Brandon's Powerflow_Code_V5_final.py - the dual-anchor algorithm
verified against the Valeo Combined_Plant_Report Excel data, reconciles all
four meter anchors to within 0.01 kWh.

Used by:
- platforms/*/processor.py to derive self-consumed and self-sufficient
  figures honestly instead of from approximations.
- shared/financial.py for accurate TOU billing (which kWh of import hit
  load directly vs went via the battery, etc).
"""

from __future__ import annotations


def _clip0(x: float) -> float:
    return x if x > 0 else 0.0


def split(pv: float, grid_import: float, export: float,
          charge: float, discharge: float, load: float) -> dict:
    """Split the six metered energy values into seven directed flows.

    All inputs and outputs are kWh, all >= 0.

    Returns:
        {
          'pv_to_load', 'pv_to_batt', 'pv_to_grid',
          'batt_to_load', 'batt_to_grid',
          'grid_to_load', 'grid_to_batt',
          'self_consumed',     # = pv_to_load + batt_to_load
          'self_sufficient',   # = (load - grid_to_load) / load
          'balance_error',     # PV + Grid + Discharge - Load - Charge - Export, should be ~0
        }
    """
    pv = _clip0(pv); grid_import = _clip0(grid_import); export = _clip0(export)
    charge = _clip0(charge); discharge = _clip0(discharge); load = _clip0(load)

    # Step 1: initial Batt_to_Grid estimate (battery covers export beyond PV surplus)
    pv_surplus = _clip0(pv - load)
    batt_to_grid = min(_clip0(export - pv_surplus), discharge)
    pv_to_grid = _clip0(export - batt_to_grid)

    # Step 2: Batt_to_Load with overflow redirect to grid
    pv_to_batt_est = min(_clip0(pv - pv_to_grid - load), charge)
    grid_to_batt_est = _clip0(charge - pv_to_batt_est)
    grid_to_load_est = _clip0(grid_import - grid_to_batt_est)
    remaining_load = _clip0(load - grid_to_load_est)

    batt_to_load_raw = _clip0(discharge - batt_to_grid)
    batt_to_load = min(batt_to_load_raw, remaining_load)
    overflow = batt_to_load_raw - batt_to_load

    # Absorb overflow into Batt_to_Grid and recompute PV_to_Grid
    batt_to_grid = batt_to_grid + overflow
    pv_to_grid = _clip0(export - batt_to_grid)

    # Step 3: final PV allocations
    pv_to_load = min(_clip0(pv - pv_to_grid), _clip0(load - batt_to_load))
    pv_to_batt = min(_clip0(pv - pv_to_load - pv_to_grid), charge)

    # Step 4: final Grid allocations (Grid anchor)
    grid_to_batt = _clip0(charge - pv_to_batt)
    grid_to_load = _clip0(grid_import - grid_to_batt)

    self_consumed = pv_to_load + batt_to_load
    self_sufficient = (load - grid_to_load) / load if load > 0 else 0.0
    balance_error = pv + grid_import + discharge - load - charge - export

    return {
        "pv_to_load":      round(pv_to_load, 4),
        "pv_to_batt":      round(pv_to_batt, 4),
        "pv_to_grid":      round(pv_to_grid, 4),
        "batt_to_load":    round(batt_to_load, 4),
        "batt_to_grid":    round(batt_to_grid, 4),
        "grid_to_load":    round(grid_to_load, 4),
        "grid_to_batt":    round(grid_to_batt, 4),
        "self_consumed":   round(self_consumed, 4),
        "self_sufficient": round(self_sufficient, 4),
        "balance_error":   round(balance_error, 4),
    }
