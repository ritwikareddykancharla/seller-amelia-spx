"""Coupled Demand/Lead-Time (s, S) Safety-Stock, N-Tier BOM Blast-Radius & Replenishment Optimizer.

Implements:
1. Exact coupled stochastic safety stock:
      SS = z * sqrt(E[L] * sigma_D^2 + E[D]^2 * sigma_L^2)
   with reorder point s = E[D]*E[L] + SS and EOQ/MOQ order-up-to level S.
2. Directed N-Tier Bill-of-Materials (BOM) component delay propagation across
   Tier-2 raw components -> Tier-1 sub-assemblies -> Tier-0 finished ASINs.
3. Side-by-side optimization of Lateral DC Rebalancing Transfers vs.
   MOQ-Constrained Split Vendor Purchase Orders vs. Expedited Air Freight.
4. Multi-DC Monte Carlo trajectory simulation comparing Naive Fixed-Reorder vs.
   Coupled (s, S) + Lateral Rebalancing.
"""
from __future__ import annotations

import math
import random
import time
from statistics import NormalDist
from typing import Any, Dict, List, Optional

from .anomaly_radar import REGIONAL_DCS, compute_unit_economics

# Realistic 3-Tier Bill-of-Materials (Tier-2 Raw Component -> Tier-1 Sub-Assembly -> Tier-0 Finished ASIN)
BOM_COMPONENTS: Dict[str, Dict[str, Any]] = {
    "COMP-BMS-IC04": {
        "node_id": "COMP-BMS-IC04",
        "tier": 2,
        "name": "Texas-Fab 16S LiFePO4 Battery Protection Controller IC",
        "supplier": "Shenzhen MicroPower Semi",
        "buffer_days": 3.0,
        "unit_cost_usd": 6.40,
        "children": [
            {"target_id": "SUB-BAT-PACK48", "qty_per_parent": 2},
            {"target_id": "SUB-MPPT-CTRL", "qty_per_parent": 1},
        ],
    },
    "COMP-CELL-21700": {
        "node_id": "COMP-CELL-21700",
        "tier": 2,
        "name": "High-Cycle LiFePO4 Cylindrical Cell 4000mAh",
        "supplier": "Busan Energy Cells Co.",
        "buffer_days": 4.0,
        "unit_cost_usd": 3.10,
        "children": [
            {"target_id": "SUB-BAT-PACK48", "qty_per_parent": 16},
        ],
    },
    "COMP-OLED-13": {
        "node_id": "COMP-OLED-13",
        "tier": 2,
        "name": "High-Contrast 1.3in Spill-Proof Micro-OLED Display",
        "supplier": "Tainan OptoDisplays Ltd.",
        "buffer_days": 2.5,
        "unit_cost_usd": 4.25,
        "children": [
            {"target_id": "SUB-DISP-HEAD", "qty_per_parent": 1},
            {"target_id": "SUB-LOAD-CELL01", "qty_per_parent": 1},
        ],
    },
    "COMP-NTC-PROBE": {
        "node_id": "COMP-NTC-PROBE",
        "tier": 2,
        "name": "Food-Grade 316L Stainless Dual-Junction Thermistor",
        "supplier": "Stuttgart Sensorik GmbH",
        "buffer_days": 3.5,
        "unit_cost_usd": 3.80,
        "children": [
            {"target_id": "SUB-DISP-HEAD", "qty_per_parent": 2},
        ],
    },
    # Tier-1 Sub-Assemblies
    "SUB-BAT-PACK48": {
        "node_id": "SUB-BAT-PACK48",
        "tier": 1,
        "name": "48V Welded LiFePO4 Core Battery Pack Module",
        "supplier": "Monterrey Pack Assembly Plant",
        "buffer_days": 2.0,
        "unit_cost_usd": 68.00,
        "children": [
            {"target_id": "SKU-PWR-101", "qty_per_parent": 1},
            {"target_id": "SKU-CAT-007", "qty_per_parent": 1},
        ],
    },
    "SUB-MPPT-CTRL": {
        "node_id": "SUB-MPPT-CTRL",
        "tier": 1,
        "name": "98%-Efficiency MPPT Solar Charge & Junction Controller",
        "supplier": "Guadalajara Power Electronics",
        "buffer_days": 1.5,
        "unit_cost_usd": 22.50,
        "children": [
            {"target_id": "SKU-SOL-108", "qty_per_parent": 1},
            {"target_id": "SKU-PWR-101", "qty_per_parent": 1},
        ],
    },
    "SUB-DISP-HEAD": {
        "node_id": "SUB-DISP-HEAD",
        "tier": 1,
        "name": "Sealed Dual-Band RF Receiver & OLED Readout Dock",
        "supplier": "Penang Smart Appliance EMS",
        "buffer_days": 2.0,
        "unit_cost_usd": 11.40,
        "children": [
            {"target_id": "SKU-KTN-204", "qty_per_parent": 1},
            {"target_id": "SKU-CAT-008", "qty_per_parent": 1},
        ],
    },
    "SUB-LOAD-CELL01": {
        "node_id": "SUB-LOAD-CELL01",
        "tier": 1,
        "name": "0.1g Strain-Gauge Bridge & Auto-Tare PCB Assembly",
        "supplier": "Suzhou Precision Weighing",
        "buffer_days": 2.0,
        "unit_cost_usd": 7.90,
        "children": [
            {"target_id": "SKU-ESP-309", "qty_per_parent": 1},
            {"target_id": "SKU-CAT-009", "qty_per_parent": 1},
        ],
    },
}


def compute_coupled_ss_policy(
    daily_demand_mean: float,
    daily_demand_std: float,
    lead_time_mean_days: float,
    lead_time_std_days: float,
    unit_cogs_usd: float,
    service_level: float = 0.975,
    order_setup_cost_usd: float = 125.0,
    annual_holding_rate: float = 0.24,
    moq_units: int = 100,
) -> Dict[str, Any]:
    """Compute coupled demand + lead-time (s, S) inventory parameters vs. naive demand-only formula."""
    clamped_sl = min(0.999, max(0.55, float(service_level)))
    z = NormalDist().inv_cdf(clamped_sl)

    mu_d = max(0.01, float(daily_demand_mean))
    sig_d = max(0.0, float(daily_demand_std))
    mu_l = max(0.5, float(lead_time_mean_days))
    sig_l = max(0.0, float(lead_time_std_days))

    demand_var_term = mu_l * (sig_d ** 2)
    lead_time_var_term = (mu_d ** 2) * (sig_l ** 2)
    coupled_std_units = math.sqrt(demand_var_term + lead_time_var_term)
    naive_std_units = math.sqrt(demand_var_term)

    ss_coupled = int(math.ceil(z * coupled_std_units))
    ss_naive = int(math.ceil(z * naive_std_units))

    expected_lead_time_demand = mu_d * mu_l
    reorder_point_s = int(math.ceil(expected_lead_time_demand + ss_coupled))
    naive_reorder_point_s = int(math.ceil(expected_lead_time_demand + ss_naive))

    annual_demand = mu_d * 365.0
    annual_holding_per_unit = max(0.40, float(unit_cogs_usd) * float(annual_holding_rate))
    eoq = int(math.ceil(math.sqrt((2.0 * annual_demand * float(order_setup_cost_usd)) / annual_holding_per_unit)))
    effective_lot_qty = max(int(moq_units), eoq)
    order_up_to_S = reorder_point_s + effective_lot_qty

    lead_time_variance_share_pct = (
        round((lead_time_var_term / (demand_var_term + lead_time_var_term)) * 100.0, 1)
        if (demand_var_term + lead_time_var_term) > 0
        else 0.0
    )

    return {
        "service_level_pct": round(clamped_sl * 100.0, 2),
        "z_score": round(z, 4),
        "expected_lead_time_demand_units": round(expected_lead_time_demand, 1),
        "coupled_safety_stock_units": ss_coupled,
        "naive_safety_stock_units": ss_naive,
        "safety_stock_under_buffer_units": max(0, ss_coupled - ss_naive),
        "lead_time_variance_share_pct": lead_time_variance_share_pct,
        "reorder_point_s": reorder_point_s,
        "naive_reorder_point_s": naive_reorder_point_s,
        "eoq_units": eoq,
        "moq_units": int(moq_units),
        "order_up_to_S": order_up_to_S,
    }


def trace_bom_blast_radius(
    component_id: str,
    supplier_delay_days: float,
    sku_catalog: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Propagate a Tier-2 or Tier-1 component delay across dependent sub-assemblies and finished ASINs."""
    t0 = time.perf_counter()
    if component_id not in BOM_COMPONENTS:
        raise KeyError(f"Unknown BOM component_id: {component_id}")

    sku_by_id = {s["sku"]: s for s in sku_catalog}
    root = BOM_COMPONENTS[component_id]
    raw_delay = max(0.0, float(supplier_delay_days))
    net_after_root_buffer = max(0.0, raw_delay - float(root["buffer_days"]))

    propagated_paths: List[Dict[str, Any]] = []
    impacted_asins: Dict[str, Dict[str, Any]] = {}

    def _walk(curr_id: str, incoming_delay_days: float, chain: List[str], mult_qty: int) -> None:
        node = BOM_COMPONENTS[curr_id]
        for edge in node["children"]:
            target = edge["target_id"]
            qty = mult_qty * int(edge["qty_per_parent"])
            if target in BOM_COMPONENTS:
                sub = BOM_COMPONENTS[target]
                sub_net_delay = max(0.0, incoming_delay_days - float(sub["buffer_days"]))
                propagated_paths.append(
                    {
                        "from_node": curr_id,
                        "to_node": target,
                        "tier": sub["tier"],
                        "buffer_days_absorbed": float(sub["buffer_days"]),
                        "propagated_delay_days": round(sub_net_delay, 2),
                        "qty_multiplier": qty,
                    }
                )
                _walk(target, sub_net_delay, chain + [target], qty)
            elif target in sku_by_id:
                sku = sku_by_id[target]
                econ = compute_unit_economics(sku)
                daily_vel = float(sku["daily_demand_mean"])
                days_cover = float(sku["on_hand_units"]) / max(0.1, daily_vel)
                eff_replenish_days = float(sku["lead_time_mean_days"]) + incoming_delay_days
                stockout_days = min(30.0, max(0.0, eff_replenish_days - days_cover))
                stalled_units = round(daily_vel * stockout_days, 1)
                rev_risk = round(stalled_units * float(sku["sale_price_usd"]), 2)
                margin_risk = round(stalled_units * max(0.5, econ["net_unit_margin_usd"]), 2)

                existing = impacted_asins.get(target)
                if existing is None or incoming_delay_days > existing["downstream_delay_days"]:
                    impacted_asins[target] = {
                        "sku": sku["sku"],
                        "asin": sku["asin"],
                        "title": sku["title"],
                        "primary_dc": sku["primary_dc"],
                        "dependency_path": " -> ".join(chain + [target]),
                        "component_units_per_asin": qty,
                        "on_hand_units": int(sku["on_hand_units"]),
                        "days_of_cover": round(days_cover, 1),
                        "base_lead_days": float(sku["lead_time_mean_days"]),
                        "downstream_delay_days": round(incoming_delay_days, 2),
                        "effective_replenish_days": round(eff_replenish_days, 1),
                        "stockout_days_30d": round(stockout_days, 1),
                        "stalled_units_30d": stalled_units,
                        "revenue_at_risk_30d_usd": rev_risk,
                        "margin_at_risk_30d_usd": margin_risk,
                    }

    _walk(component_id, net_after_root_buffer, [component_id], 1)

    ranked_asins = sorted(
        impacted_asins.values(),
        key=lambda x: x["margin_at_risk_30d_usd"],
        reverse=True,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    return {
        "component_id": component_id,
        "component_name": root["name"],
        "tier": root["tier"],
        "supplier": root["supplier"],
        "supplier_delay_days": raw_delay,
        "root_buffer_days": float(root["buffer_days"]),
        "net_propagated_delay_days": round(net_after_root_buffer, 2),
        "sub_assembly_hops": propagated_paths,
        "impacted_finished_skus_count": len(ranked_asins),
        "total_stalled_units_30d": round(sum(x["stalled_units_30d"] for x in ranked_asins), 1),
        "total_revenue_at_risk_30d_usd": round(sum(x["revenue_at_risk_30d_usd"] for x in ranked_asins), 2),
        "total_margin_at_risk_30d_usd": round(sum(x["margin_at_risk_30d_usd"] for x in ranked_asins), 2),
        "impacted_asins": ranked_asins,
        "compute_ms": elapsed_ms,
    }


def optimize_replenishment_plan(
    sku: Dict[str, Any],
    supplier_delay_days: float = 12.0,
    service_level: float = 0.975,
    deficit_dc: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare Lateral DC Rebalancing Transfers vs. MOQ-Constrained Split Vendor PO vs. Expedited Air Freight."""
    t0 = time.perf_counter()
    target_dc = deficit_dc or sku["primary_dc"]
    dc_inv = dict(sku.get("dc_inventory") or {d: sku["on_hand_units"] for d in REGIONAL_DCS})
    local_on_hand = int(dc_inv.get(target_dc, sku["on_hand_units"]))

    econ = compute_unit_economics(sku)
    unit_margin = max(1.0, econ["net_unit_margin_usd"])
    cogs = float(sku["unit_cogs_usd"])
    daily_vel = float(sku["daily_demand_mean"])
    daily_std = float(sku["daily_demand_std"])
    base_lt = float(sku["lead_time_mean_days"])
    lt_std = float(sku["lead_time_std_days"])
    moq = int(sku.get("moq_units", 120))
    cu_ft = float(sku.get("cubic_feet_per_unit", 0.45))

    ss_policy = compute_coupled_ss_policy(
        daily_demand_mean=daily_vel,
        daily_demand_std=daily_std,
        lead_time_mean_days=base_lt,
        lead_time_std_days=lt_std,
        unit_cogs_usd=cogs,
        service_level=service_level,
        moq_units=moq,
    )

    eff_primary_lt = base_lt + max(0.0, float(supplier_delay_days))
    days_cover = local_on_hand / max(0.1, daily_vel)
    unmitigated_stockout_days = min(30.0, max(0.0, eff_primary_lt - days_cover))
    unmitigated_lost_units = round(unmitigated_stockout_days * daily_vel, 1)
    unmitigated_margin_loss = round(unmitigated_lost_units * unit_margin, 2)

    target_replenish_qty = max(
        int(math.ceil(unmitigated_lost_units + 0.5 * ss_policy["coupled_safety_stock_units"])),
        int(math.ceil(daily_vel * 10.0)),
    )

    # Check transferable surplus across sister DCs (preserving 12 days local cover at donor DCs)
    donor_reserve_units = int(math.ceil(daily_vel * 0.35 * 12.0))
    donor_transfers: List[Dict[str, Any]] = []
    total_surplus_available = 0
    for dc_name, qty in dc_inv.items():
        if dc_name == target_dc:
            continue
        surplus = max(0, int(qty) - donor_reserve_units)
        if surplus > 0:
            donor_transfers.append({"donor_dc": dc_name, "available_surplus_units": surplus})
            total_surplus_available += surplus

    donor_transfers.sort(key=lambda x: x["available_surplus_units"], reverse=True)

    # Option 1: Lateral DC Rebalancing Transfer (2.5-day ground inter-DC transshipment, zero new vendor COGS lockup)
    transfer_qty = min(target_replenish_qty, total_surplus_available)
    per_unit_transfer_freight = round(0.85 + cu_ft * 0.95, 2)
    transfer_arrival_days = 3.0
    gap_days_opt1 = max(0.0, transfer_arrival_days - days_cover)
    covered_days_after_transfer = (local_on_hand + transfer_qty) / max(0.1, daily_vel)
    residual_stockout_days_opt1 = gap_days_opt1 + max(0.0, min(30.0, eff_primary_lt) - max(transfer_arrival_days, covered_days_after_transfer))
    residual_lost_units_opt1 = round(residual_stockout_days_opt1 * daily_vel, 1)
    logistics_cost_opt1 = round((transfer_qty * per_unit_transfer_freight) + (65.0 if transfer_qty > 0 else 0.0), 2)
    stockout_penalty_opt1 = round(residual_lost_units_opt1 * unit_margin, 2)
    total_effective_cost_opt1 = round(logistics_cost_opt1 + stockout_penalty_opt1, 2)
    margin_preserved_opt1 = round(max(0.0, unmitigated_margin_loss - stockout_penalty_opt1), 2)

    # Option 2: MOQ-Constrained Split Vendor Purchase Order (50% Backup Nearshore Supplier @ 7d +7% COGS, 50% Primary)
    split_backup_qty = max(moq, int(math.ceil(target_replenish_qty * 0.65)))
    backup_arrival_days = 7.5
    backup_cogs_premium_per_unit = round(cogs * 0.075, 2)
    backup_ground_freight_per_unit = round(1.35 + cu_ft * 1.15, 2)
    excess_moq_units = max(0, split_backup_qty - target_replenish_qty)
    moq_holding_drag = round(excess_moq_units * cogs * 0.04, 2)
    residual_stockout_days_opt2 = min(30.0, max(0.0, backup_arrival_days - days_cover))
    residual_lost_units_opt2 = round(residual_stockout_days_opt2 * daily_vel, 1)
    logistics_cost_opt2 = round(
        140.0
        + split_backup_qty * (backup_cogs_premium_per_unit + backup_ground_freight_per_unit)
        + moq_holding_drag,
        2,
    )
    stockout_penalty_opt2 = round(residual_lost_units_opt2 * unit_margin, 2)
    total_effective_cost_opt2 = round(logistics_cost_opt2 + stockout_penalty_opt2, 2)
    margin_preserved_opt2 = round(max(0.0, unmitigated_margin_loss - stockout_penalty_opt2), 2)

    # Option 3: Expedited Air Freight (4.0-day air cargo charter from primary vendor, high per-unit air surcharge)
    air_qty = max(int(math.ceil(moq * 0.5)), target_replenish_qty)
    air_arrival_days = 4.0
    air_freight_per_unit = round(5.20 + cu_ft * 6.40, 2)
    residual_stockout_days_opt3 = min(30.0, max(0.0, air_arrival_days - days_cover))
    residual_lost_units_opt3 = round(residual_stockout_days_opt3 * daily_vel, 1)
    logistics_cost_opt3 = round(290.0 + air_qty * air_freight_per_unit, 2)
    stockout_penalty_opt3 = round(residual_lost_units_opt3 * unit_margin, 2)
    total_effective_cost_opt3 = round(logistics_cost_opt3 + stockout_penalty_opt3, 2)
    margin_preserved_opt3 = round(max(0.0, unmitigated_margin_loss - stockout_penalty_opt3), 2)

    options = [
        {
            "strategy_id": "LATERAL_DC_TRANSFER",
            "title": "Lateral DC Rebalancing Transfer",
            "units_dispatched": transfer_qty,
            "arrival_days": transfer_arrival_days,
            "moq_applied": 0,
            "incremental_logistics_and_premium_usd": logistics_cost_opt1,
            "residual_stockout_units": residual_lost_units_opt1,
            "residual_stockout_margin_loss_usd": stockout_penalty_opt1,
            "total_risk_adjusted_cost_usd": total_effective_cost_opt1,
            "margin_preserved_usd": margin_preserved_opt1,
            "net_benefit_vs_do_nothing_usd": round(unmitigated_margin_loss - total_effective_cost_opt1, 2),
            "donor_allocation": donor_transfers[:2],
        },
        {
            "strategy_id": "SPLIT_VENDOR_PO",
            "title": "MOQ-Constrained Split Vendor PO",
            "units_dispatched": split_backup_qty,
            "arrival_days": backup_arrival_days,
            "moq_applied": moq,
            "incremental_logistics_and_premium_usd": logistics_cost_opt2,
            "residual_stockout_units": residual_lost_units_opt2,
            "residual_stockout_margin_loss_usd": stockout_penalty_opt2,
            "total_risk_adjusted_cost_usd": total_effective_cost_opt2,
            "margin_preserved_usd": margin_preserved_opt2,
            "net_benefit_vs_do_nothing_usd": round(unmitigated_margin_loss - total_effective_cost_opt2, 2),
            "donor_allocation": [],
        },
        {
            "strategy_id": "EXPEDITED_AIR_FREIGHT",
            "title": "Expedited Air Freight Charter",
            "units_dispatched": air_qty,
            "arrival_days": air_arrival_days,
            "moq_applied": int(math.ceil(moq * 0.5)),
            "incremental_logistics_and_premium_usd": logistics_cost_opt3,
            "residual_stockout_units": residual_lost_units_opt3,
            "residual_stockout_margin_loss_usd": stockout_penalty_opt3,
            "total_risk_adjusted_cost_usd": total_effective_cost_opt3,
            "margin_preserved_usd": margin_preserved_opt3,
            "net_benefit_vs_do_nothing_usd": round(unmitigated_margin_loss - total_effective_cost_opt3, 2),
            "donor_allocation": [],
        },
    ]

    options.sort(key=lambda x: x["total_risk_adjusted_cost_usd"])
    best = options[0]
    runner_up = options[1]

    return {
        "sku": sku["sku"],
        "asin": sku["asin"],
        "title": sku["title"],
        "deficit_dc": target_dc,
        "local_on_hand_units": local_on_hand,
        "days_of_cover": round(days_cover, 1),
        "effective_primary_lead_days": round(eff_primary_lt, 1),
        "unmitigated_stockout_days_30d": round(unmitigated_stockout_days, 1),
        "unmitigated_margin_loss_usd": unmitigated_margin_loss,
        "safety_stock_policy": ss_policy,
        "recommended_strategy": best["strategy_id"],
        "recommended_title": best["title"],
        "cost_advantage_vs_next_best_usd": round(
            runner_up["total_risk_adjusted_cost_usd"] - best["total_risk_adjusted_cost_usd"],
            2,
        ),
        "options": options,
        "compute_ms": round((time.perf_counter() - t0) * 1000.0, 3),
    }


def run_monte_carlo_policy_comparison(
    sku_catalog: List[Dict[str, Any]],
    num_trials: int = 120,
    horizon_days: int = 60,
    seed: int = 2026,
) -> Dict[str, Any]:
    """Run stochastic demand + variable vendor lead-time trajectories comparing Naive vs. Coupled (s, S) + Lateral."""
    t0 = time.perf_counter()
    sample_skus = sku_catalog[:12]

    def _simulate(coupled_and_lateral: bool) -> Dict[str, float]:
        rng = random.Random(seed)
        total_demanded = 0.0
        total_filled = 0.0
        stockout_dc_days = 0
        total_dc_days = 0
        total_landed_cost = 0.0

        for _ in range(num_trials):
            for sku in sample_skus:
                econ = compute_unit_economics(sku)
                unit_margin = max(2.0, econ["net_unit_margin_usd"])
                cogs = float(sku["unit_cogs_usd"])
                mu_d = float(sku["daily_demand_mean"]) * 0.45
                sig_d = float(sku["daily_demand_std"]) * 0.45
                mu_l = float(sku["lead_time_mean_days"])
                sig_l = float(sku["lead_time_std_days"]) + 2.2
                moq = max(40, int(sku["moq_units"] // 2))

                pol = compute_coupled_ss_policy(
                    daily_demand_mean=mu_d,
                    daily_demand_std=sig_d,
                    lead_time_mean_days=mu_l,
                    lead_time_std_days=sig_l if coupled_and_lateral else 0.0,
                    unit_cogs_usd=cogs,
                    service_level=0.975,
                    moq_units=moq,
                )
                s_pt = pol["reorder_point_s"] if coupled_and_lateral else pol["naive_reorder_point_s"]
                S_up = s_pt + max(moq, pol["eoq_units"])

                # Simulate 2 regional DCs (West & Midwest) with asymmetric demand shocks
                on_hand = [int(s_pt * 0.95), int(s_pt * 1.35)]
                pipeline: List[List[List[int]]] = [[], []]  # [arrival_day, qty]

                for day in range(horizon_days):
                    for dc_idx in (0, 1):
                        arrived = sum(q for arr_d, q in pipeline[dc_idx] if arr_d <= day)
                        pipeline[dc_idx] = [[arr_d, q] for arr_d, q in pipeline[dc_idx] if arr_d > day]
                        on_hand[dc_idx] += arrived

                    # Lateral rebalancing before daily fulfillment if one DC is below 3 days cover & peer has surplus
                    if coupled_and_lateral:
                        for dc_idx in (0, 1):
                            peer = 1 - dc_idx
                            if on_hand[dc_idx] < int(mu_d * 3.0) and on_hand[peer] > (s_pt + int(mu_d * 3.0)):
                                xfer = min(int(mu_d * 5.0), on_hand[peer] - s_pt)
                                if xfer > 0:
                                    on_hand[peer] -= xfer
                                    pipeline[dc_idx].append([day + 2, xfer])
                                    total_landed_cost += 35.0 + xfer * 1.15

                    for dc_idx in (0, 1):
                        total_dc_days += 1
                        d_draw = max(0.0, rng.gauss(mu_d, sig_d))
                        dem = int(round(d_draw))
                        total_demanded += dem
                        filled = min(on_hand[dc_idx], dem)
                        unmet = dem - filled
                        on_hand[dc_idx] -= filled
                        total_filled += filled
                        if unmet > 0:
                            stockout_dc_days += 1
                            total_landed_cost += unmet * unit_margin

                        # Daily holding cost
                        total_landed_cost += on_hand[dc_idx] * (cogs * 0.24 / 365.0)

                        # Inventory position check -> trigger vendor PO
                        inv_pos = on_hand[dc_idx] + sum(q for _, q in pipeline[dc_idx])
                        if inv_pos <= s_pt:
                            order_q = max(moq, S_up - inv_pos)
                            # Sample positive skewed vendor lead time (including occasional port/component slip)
                            slip = rng.choice([0, 0, 0, 4, 7])
                            lt_days = max(3, int(round(rng.gauss(mu_l, sig_l) + slip)))
                            pipeline[dc_idx].append([day + lt_days, order_q])
                            total_landed_cost += 110.0 + order_q * 0.85

        stockout_day_rate_pct = round((stockout_dc_days / max(1, total_dc_days)) * 100.0, 2)
        fill_rate_pct = round((total_filled / max(1.0, total_demanded)) * 100.0, 2)
        mean_trial_cost = round(total_landed_cost / max(1, num_trials), 2)
        return {
            "stockout_day_rate_pct": stockout_day_rate_pct,
            "unit_fill_rate_pct": fill_rate_pct,
            "mean_portfolio_cost_per_trial_usd": mean_trial_cost,
        }

    naive_res = _simulate(coupled_and_lateral=False)
    coupled_res = _simulate(coupled_and_lateral=True)

    stockout_rel_reduction_pct = round(
        ((naive_res["stockout_day_rate_pct"] - coupled_res["stockout_day_rate_pct"]) / max(0.01, naive_res["stockout_day_rate_pct"])) * 100.0,
        1,
    )
    cost_savings_per_trial = round(
        naive_res["mean_portfolio_cost_per_trial_usd"] - coupled_res["mean_portfolio_cost_per_trial_usd"],
        2,
    )
    cost_reduction_pct = round(
        (cost_savings_per_trial / max(1.0, naive_res["mean_portfolio_cost_per_trial_usd"])) * 100.0,
        1,
    )

    return {
        "trials": num_trials,
        "horizon_days": horizon_days,
        "skus_per_trial": len(sample_skus),
        "naive_fixed_reorder": naive_res,
        "coupled_ss_plus_lateral": coupled_res,
        "relative_stockout_reduction_pct": stockout_rel_reduction_pct,
        "fill_rate_gain_pct_points": round(
            coupled_res["unit_fill_rate_pct"] - naive_res["unit_fill_rate_pct"], 2
        ),
        "cost_savings_per_trial_usd": cost_savings_per_trial,
        "cost_reduction_pct": cost_reduction_pct,
        "simulation_ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }
