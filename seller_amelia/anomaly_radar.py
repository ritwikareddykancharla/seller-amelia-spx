"""Proactive Dollar-Impact Seller & Supply-Chain Anomaly Detector.

Scans multi-SKU merchant and warehouse telemetry across five operational anomaly
classes (listing suppressions, Featured Offer / Buy Box losses, NCX customer
return spikes, FBA 181-365+ day aged inventory surcharge cliffs, and upstream
vendor lead-time slips) and ranks every issue strictly by expected 30-day dollar
contribution margin and revenue exposure rather than raw alert count.
"""
from __future__ import annotations

import copy
import random
import time
from typing import Any, Dict, List, Optional

REGIONAL_DCS = ("ONT8-West", "MDW2-Midwest", "TEB9-East", "DAL3-South")

CATEGORIES = (
    "Portable Power & Solar",
    "Smart Kitchen & Precision Appliances",
    "Home Espresso & Specialty Coffee",
    "Outdoor Living & Modular Patio",
)


def compute_unit_economics(
    sku: Dict[str, Any],
    override_price_usd: Optional[float] = None,
) -> Dict[str, float]:
    """Compute exact per-unit Seller Central / FBA contribution margin."""
    price = round(float(override_price_usd if override_price_usd is not None else sku["sale_price_usd"]), 2)
    cogs = float(sku["unit_cogs_usd"])
    referral_rate = float(sku.get("referral_fee_rate", 0.15))
    referral_fee = round(price * referral_rate, 2)
    fba_fee = float(sku["fba_fulfillment_fee_usd"])
    storage_fee = float(sku.get("monthly_storage_fee_usd", 0.55))
    return_rate = float(sku.get("return_rate_pct", 3.5)) / 100.0
    return_proc_fee = float(sku.get("return_processing_fee_usd", 4.50))
    unsellable_share = float(sku.get("unsellable_return_fraction", 0.35))

    # Expected per-sold-unit return drag: return processing + unsellable COGS loss + 20% referral admin retention
    expected_return_drag = round(
        return_rate * (return_proc_fee + unsellable_share * cogs + 0.20 * referral_fee),
        2,
    )
    total_variable_cost = round(cogs + referral_fee + fba_fee + storage_fee + expected_return_drag, 2)
    net_margin_usd = round(price - total_variable_cost, 2)
    net_margin_pct = round((net_margin_usd / price) * 100.0, 2) if price > 0 else -100.0

    return {
        "sale_price_usd": price,
        "unit_cogs_usd": round(cogs, 2),
        "referral_fee_usd": referral_fee,
        "fba_fulfillment_fee_usd": round(fba_fee, 2),
        "monthly_storage_fee_usd": round(storage_fee, 2),
        "expected_return_drag_usd": expected_return_drag,
        "total_variable_cost_usd": total_variable_cost,
        "net_unit_margin_usd": net_margin_usd,
        "net_margin_pct": net_margin_pct,
    }


def _flagship_skus() -> List[Dict[str, Any]]:
    """Curated flagship ASINs mirroring real Seller Central & multi-DC supply chain states."""
    return [
        {
            "sku": "SKU-PWR-101",
            "asin": "B09PWR0101",
            "title": "VoltPeak 600W LiFePO4 Portable Power Station",
            "category": "Portable Power & Solar",
            "primary_dc": "ONT8-West",
            "sale_price_usd": 299.00,
            "competitor_price_usd": 304.00,
            "unit_cogs_usd": 116.00,
            "referral_fee_rate": 0.08,
            "fba_fulfillment_fee_usd": 16.40,
            "monthly_storage_fee_usd": 1.85,
            "return_processing_fee_usd": 9.80,
            "unsellable_return_fraction": 0.30,
            "daily_demand_mean": 28.0,
            "daily_demand_std": 6.4,
            "lead_time_mean_days": 16.0,
            "lead_time_std_days": 3.5,
            "current_vendor_lead_days": 16.0,
            "on_hand_units": 520,
            "dc_inventory": {"ONT8-West": 520, "MDW2-Midwest": 290, "TEB9-East": 310, "DAL3-South": 185},
            "moq_units": 120,
            "cubic_feet_per_unit": 0.92,
            "listing_status": "SUPPRESSED",
            "suppression_reason": "Missing required hazmat attribute: lithium_battery_energy_content (Wh) and UN38.3 test summary ID",
            "expected_unresolved_days": 14.0,
            "buy_box_win_pct": 0.0,
            "baseline_buy_box_pct": 94.0,
            "return_rate_pct": 3.1,
            "baseline_return_rate_pct": 2.9,
            "top_ncx_reason": "Normal baseline questions on solar MC4 adapter polarity",
            "aged_181_270_units": 0,
            "aged_271_plus_units": 0,
            "age_days_max": 64,
            "days_to_surcharge_cliff": 99,
            "raw_alert_count": 1,
            "min_margin_floor_pct": 18.0,
        },
        {
            "sku": "SKU-KTN-204",
            "asin": "B08THERM24",
            "title": "ChefProbe Dual-Band Wireless Meat Thermometer Pro",
            "category": "Smart Kitchen & Precision Appliances",
            "primary_dc": "MDW2-Midwest",
            "sale_price_usd": 79.00,
            "competitor_price_usd": 75.50,
            "unit_cogs_usd": 21.50,
            "referral_fee_rate": 0.15,
            "fba_fulfillment_fee_usd": 5.35,
            "monthly_storage_fee_usd": 0.42,
            "return_processing_fee_usd": 3.85,
            "unsellable_return_fraction": 0.35,
            "daily_demand_mean": 64.0,
            "daily_demand_std": 14.2,
            "lead_time_mean_days": 12.0,
            "lead_time_std_days": 2.8,
            "current_vendor_lead_days": 12.0,
            "on_hand_units": 1180,
            "dc_inventory": {"ONT8-West": 380, "MDW2-Midwest": 1180, "TEB9-East": 510, "DAL3-South": 290},
            "moq_units": 250,
            "cubic_feet_per_unit": 0.14,
            "listing_status": "ACTIVE",
            "suppression_reason": "",
            "expected_unresolved_days": 0.0,
            "buy_box_win_pct": 22.0,
            "baseline_buy_box_pct": 93.0,
            "return_rate_pct": 3.4,
            "baseline_return_rate_pct": 3.2,
            "top_ncx_reason": "Occasional Bluetooth pairing question",
            "aged_181_270_units": 0,
            "aged_271_plus_units": 0,
            "age_days_max": 48,
            "days_to_surcharge_cliff": 99,
            "raw_alert_count": 2,
            "min_margin_floor_pct": 18.0,
        },
        {
            "sku": "SKU-SOL-108",
            "asin": "B09SOL0108",
            "title": "SunFold 200W Monocrystalline Folding Solar Kit",
            "category": "Portable Power & Solar",
            "primary_dc": "ONT8-West",
            "sale_price_usd": 219.00,
            "competitor_price_usd": 224.00,
            "unit_cogs_usd": 84.00,
            "referral_fee_rate": 0.08,
            "fba_fulfillment_fee_usd": 13.20,
            "monthly_storage_fee_usd": 1.40,
            "return_processing_fee_usd": 7.90,
            "unsellable_return_fraction": 0.28,
            "daily_demand_mean": 19.0,
            "daily_demand_std": 5.1,
            "lead_time_mean_days": 14.0,
            "lead_time_std_days": 4.8,
            "current_vendor_lead_days": 27.0,
            "on_hand_units": 190,
            "dc_inventory": {"ONT8-West": 190, "MDW2-Midwest": 465, "TEB9-East": 340, "DAL3-South": 160},
            "moq_units": 150,
            "cubic_feet_per_unit": 0.78,
            "listing_status": "ACTIVE",
            "suppression_reason": "",
            "expected_unresolved_days": 0.0,
            "buy_box_win_pct": 91.0,
            "baseline_buy_box_pct": 92.0,
            "return_rate_pct": 2.9,
            "baseline_return_rate_pct": 2.8,
            "top_ncx_reason": "Kickstand angle stiffness",
            "aged_181_270_units": 0,
            "aged_271_plus_units": 0,
            "age_days_max": 52,
            "days_to_surcharge_cliff": 99,
            "raw_alert_count": 2,
            "min_margin_floor_pct": 18.0,
        },
        {
            "sku": "SKU-ESP-309",
            "asin": "B08BREW309",
            "title": "CremaLab Precision 0.1g Barista Espresso Scale",
            "category": "Home Espresso & Specialty Coffee",
            "primary_dc": "TEB9-East",
            "sale_price_usd": 54.00,
            "competitor_price_usd": 55.00,
            "unit_cogs_usd": 14.20,
            "referral_fee_rate": 0.15,
            "fba_fulfillment_fee_usd": 4.75,
            "monthly_storage_fee_usd": 0.35,
            "return_processing_fee_usd": 3.60,
            "unsellable_return_fraction": 0.48,
            "daily_demand_mean": 48.0,
            "daily_demand_std": 10.5,
            "lead_time_mean_days": 11.0,
            "lead_time_std_days": 2.4,
            "current_vendor_lead_days": 11.0,
            "on_hand_units": 890,
            "dc_inventory": {"ONT8-West": 310, "MDW2-Midwest": 290, "TEB9-East": 890, "DAL3-South": 240},
            "moq_units": 300,
            "cubic_feet_per_unit": 0.11,
            "listing_status": "ACTIVE",
            "suppression_reason": "",
            "expected_unresolved_days": 0.0,
            "buy_box_win_pct": 89.0,
            "baseline_buy_box_pct": 91.0,
            "return_rate_pct": 15.8,
            "baseline_return_rate_pct": 2.9,
            "top_ncx_reason": "Lot 26B missing silicone thermal pad & USB-C charging cable in inner carton (89 verbatims)",
            "aged_181_270_units": 0,
            "aged_271_plus_units": 0,
            "age_days_max": 61,
            "days_to_surcharge_cliff": 99,
            "raw_alert_count": 5,
            "min_margin_floor_pct": 18.0,
        },
        {
            "sku": "SKU-PAT-412",
            "asin": "B07PATIO12",
            "title": "CedarCrest Modular Teak Patio Lounger Set",
            "category": "Outdoor Living & Modular Patio",
            "primary_dc": "DAL3-South",
            "sale_price_usd": 349.00,
            "competitor_price_usd": 349.00,
            "unit_cogs_usd": 152.00,
            "referral_fee_rate": 0.15,
            "fba_fulfillment_fee_usd": 28.50,
            "monthly_storage_fee_usd": 4.60,
            "return_processing_fee_usd": 18.00,
            "unsellable_return_fraction": 0.40,
            "daily_demand_mean": 2.1,
            "daily_demand_std": 0.9,
            "lead_time_mean_days": 21.0,
            "lead_time_std_days": 4.5,
            "current_vendor_lead_days": 21.0,
            "on_hand_units": 485,
            "dc_inventory": {"ONT8-West": 60, "MDW2-Midwest": 45, "TEB9-East": 55, "DAL3-South": 485},
            "moq_units": 60,
            "cubic_feet_per_unit": 3.40,
            "listing_status": "ACTIVE",
            "suppression_reason": "",
            "expected_unresolved_days": 0.0,
            "buy_box_win_pct": 88.0,
            "baseline_buy_box_pct": 90.0,
            "return_rate_pct": 4.6,
            "baseline_return_rate_pct": 4.4,
            "top_ncx_reason": "Box corner scuffing in LTL transit",
            "aged_181_270_units": 140,
            "aged_271_plus_units": 290,
            "age_days_max": 274,
            "days_to_surcharge_cliff": 4,
            "raw_alert_count": 3,
            "min_margin_floor_pct": 15.0,
        },
        {
            "sku": "SKU-ACC-901",
            "asin": "B07CLIP901",
            "title": "Adhesive Silicone Cable Organizer Clips (12-Pack)",
            "category": "Portable Power & Solar",
            "primary_dc": "MDW2-Midwest",
            "sale_price_usd": 9.99,
            "competitor_price_usd": 9.49,
            "unit_cogs_usd": 1.85,
            "referral_fee_rate": 0.15,
            "fba_fulfillment_fee_usd": 3.22,
            "monthly_storage_fee_usd": 0.08,
            "return_processing_fee_usd": 2.10,
            "unsellable_return_fraction": 0.50,
            "daily_demand_mean": 4.5,
            "daily_demand_std": 1.4,
            "lead_time_mean_days": 10.0,
            "lead_time_std_days": 1.5,
            "current_vendor_lead_days": 10.0,
            "on_hand_units": 620,
            "dc_inventory": {"ONT8-West": 180, "MDW2-Midwest": 620, "TEB9-East": 210, "DAL3-South": 140},
            "moq_units": 500,
            "cubic_feet_per_unit": 0.03,
            "listing_status": "ACTIVE",
            "suppression_reason": "",
            "expected_unresolved_days": 0.0,
            "buy_box_win_pct": 74.0,
            "baseline_buy_box_pct": 89.0,
            "return_rate_pct": 4.8,
            "baseline_return_rate_pct": 3.9,
            "top_ncx_reason": "Adhesive pad weak on textured wood",
            "aged_181_270_units": 0,
            "aged_271_plus_units": 0,
            "age_days_max": 72,
            "days_to_surcharge_cliff": 99,
            # High noisy alert count (image alt-tag warnings, minor buy-box pings) but tiny dollar exposure!
            "raw_alert_count": 46,
            "min_margin_floor_pct": 15.0,
        },
    ]


def build_default_sku_catalog(num_skus: int = 120, seed: int = 42) -> List[Dict[str, Any]]:
    """Build a deterministic 120-SKU multi-DC Seller Central & supply chain catalog."""
    rng = random.Random(seed)
    catalog = copy.deepcopy(_flagship_skus())

    idx = len(catalog) + 1
    # Add 18 more realistic anomalies with varying dollar impacts and noisy alert counts, plus healthy SKUs up to 120
    anomaly_modes = [
        "listing_suppression",
        "buy_box_loss",
        "vendor_lead_time_slip",
        "ncx_return_spike",
        "aged_fba_surcharge",
        "noisy_low_dollar",
    ]

    while len(catalog) < num_skus:
        cat = CATEGORIES[idx % len(CATEGORIES)]
        dc = REGIONAL_DCS[idx % len(REGIONAL_DCS)]
        is_anomaly_slot = len(catalog) < 28
        mode = anomaly_modes[idx % len(anomaly_modes)] if is_anomaly_slot else "healthy"

        if mode == "noisy_low_dollar":
            price = round(rng.uniform(10.99, 16.99), 2)
            cogs = round(price * rng.uniform(0.22, 0.30), 2)
            fba_fee = round(rng.uniform(3.15, 4.10), 2)
            daily_mean = round(rng.uniform(2.0, 5.5), 1)
        else:
            price = round(rng.uniform(34.0, 249.0), 2)
            cogs = round(price * rng.uniform(0.32, 0.44), 2)
            fba_fee = round(3.80 + price * 0.048, 2)
            daily_mean = round(rng.uniform(6.0, 34.0), 1)

        daily_std = round(daily_mean * rng.uniform(0.18, 0.32), 2)
        lt_mean = round(rng.uniform(10.0, 18.0), 1)
        lt_std = round(rng.uniform(2.0, 4.8), 2)
        on_hand = int(round(daily_mean * rng.uniform(22.0, 38.0)))
        cub_ft = round(rng.uniform(0.08, 1.45), 2)

        sku_rec: Dict[str, Any] = {
            "sku": f"SKU-CAT-{idx:03d}",
            "asin": f"B09AMZ{idx:04d}",
            "title": f"{cat.split()[0]} Pro Series Item #{idx:03d}",
            "category": cat,
            "primary_dc": dc,
            "sale_price_usd": price,
            "competitor_price_usd": round(price * 1.02, 2),
            "unit_cogs_usd": cogs,
            "referral_fee_rate": 0.08 if "Power" in cat else 0.15,
            "fba_fulfillment_fee_usd": fba_fee,
            "monthly_storage_fee_usd": round(cub_ft * 0.87, 2),
            "return_processing_fee_usd": round(3.10 + cub_ft * 2.2, 2),
            "unsellable_return_fraction": 0.34,
            "daily_demand_mean": daily_mean,
            "daily_demand_std": daily_std,
            "lead_time_mean_days": lt_mean,
            "lead_time_std_days": lt_std,
            "current_vendor_lead_days": lt_mean,
            "on_hand_units": on_hand,
            "dc_inventory": {
                d: (on_hand if d == dc else int(round(daily_mean * rng.uniform(8.0, 24.0))))
                for d in REGIONAL_DCS
            },
            "moq_units": int(rng.choice([80, 120, 150, 200, 250])),
            "cubic_feet_per_unit": cub_ft,
            "listing_status": "ACTIVE",
            "suppression_reason": "",
            "expected_unresolved_days": 0.0,
            "buy_box_win_pct": round(rng.uniform(88.0, 96.0), 1),
            "baseline_buy_box_pct": 92.0,
            "return_rate_pct": round(rng.uniform(2.4, 3.8), 1),
            "baseline_return_rate_pct": 3.1,
            "top_ncx_reason": "Standard fit & packaging feedback",
            "aged_181_270_units": 0,
            "aged_271_plus_units": 0,
            "age_days_max": rng.randint(24, 115),
            "days_to_surcharge_cliff": 99,
            "raw_alert_count": rng.randint(0, 2),
            "min_margin_floor_pct": 16.0,
        }

        if mode == "listing_suppression":
            sku_rec["listing_status"] = "SUPPRESSED"
            sku_rec["suppression_reason"] = "Missing mandatory UL/FCC compliance attribute or main image background defect"
            sku_rec["expected_unresolved_days"] = round(rng.uniform(7.0, 13.0), 1)
            sku_rec["buy_box_win_pct"] = 0.0
            sku_rec["raw_alert_count"] = rng.randint(1, 2)
        elif mode == "buy_box_loss":
            undercut = round(price * rng.uniform(0.03, 0.06), 2)
            sku_rec["competitor_price_usd"] = round(price - undercut, 2)
            sku_rec["buy_box_win_pct"] = round(rng.uniform(24.0, 42.0), 1)
            sku_rec["raw_alert_count"] = rng.randint(2, 4)
        elif mode == "vendor_lead_time_slip":
            cover_days = round(rng.uniform(7.0, 11.0), 1)
            sku_rec["on_hand_units"] = max(12, int(round(daily_mean * cover_days)))
            sku_rec["dc_inventory"][dc] = sku_rec["on_hand_units"]
            sku_rec["current_vendor_lead_days"] = round(lt_mean + rng.uniform(8.0, 14.0), 1)
            sku_rec["raw_alert_count"] = rng.randint(1, 3)
        elif mode == "ncx_return_spike":
            sku_rec["return_rate_pct"] = round(rng.uniform(11.5, 16.8), 1)
            sku_rec["top_ncx_reason"] = "Batch QC seal defect & misleading dimension chart in bullet #3"
            sku_rec["raw_alert_count"] = rng.randint(3, 6)
        elif mode == "aged_fba_surcharge":
            sku_rec["daily_demand_mean"] = round(rng.uniform(1.5, 3.8), 1)
            sku_rec["cubic_feet_per_unit"] = round(rng.uniform(1.4, 2.8), 2)
            sku_rec["aged_181_270_units"] = rng.randint(90, 180)
            sku_rec["aged_271_plus_units"] = rng.randint(120, 260)
            sku_rec["age_days_max"] = rng.randint(266, 310)
            sku_rec["days_to_surcharge_cliff"] = rng.randint(3, 9)
            sku_rec["raw_alert_count"] = rng.randint(2, 5)
        elif mode == "noisy_low_dollar":
            sku_rec["competitor_price_usd"] = round(price - 0.35, 2)
            sku_rec["buy_box_win_pct"] = 68.0
            sku_rec["raw_alert_count"] = rng.randint(28, 52)

        catalog.append(sku_rec)
        idx += 1

    return catalog


def evaluate_sku_anomaly(sku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Quantify 30-day dollar margin & revenue exposure for a single SKU."""
    econ = compute_unit_economics(sku)
    price = econ["sale_price_usd"]
    # Baseline healthy unit margin (before return spike penalty if return spiked)
    baseline_sku = dict(sku, return_rate_pct=sku.get("baseline_return_rate_pct", 3.0))
    base_econ = compute_unit_economics(baseline_sku)
    unit_margin = max(0.50, base_econ["net_unit_margin_usd"])
    daily_units = float(sku["daily_demand_mean"])

    anomaly_type: Optional[str] = None
    revenue_at_risk_30d = 0.0
    margin_at_risk_30d = 0.0
    surcharge_exposure_30d = 0.0
    recommended_specialist = "SupervisorAgent"
    recommended_action = "NO_ACTION"
    summary = ""

    # 1. Listing Suppression (halts active sales velocity until compliance patch lands)
    if sku.get("listing_status") == "SUPPRESSED":
        anomaly_type = "listing_suppression"
        days_lost = min(30.0, max(1.0, float(sku.get("expected_unresolved_days", 10.0))))
        lost_units = daily_units * days_lost
        revenue_at_risk_30d = round(lost_units * price, 2)
        margin_at_risk_30d = round(lost_units * unit_margin, 2)
        recommended_specialist = "CatalogComplianceAgent"
        recommended_action = "PATCH_CATALOG_ATTRIBUTES"
        summary = (
            f"Listing suppressed ({sku.get('suppression_reason')}); halting {daily_units:.1f} units/day "
            f"(${margin_at_risk_30d:,.2f} net margin & ${revenue_at_risk_30d:,.2f} revenue over {days_lost:.0f}d)."
        )

    # 2. Upstream Vendor Lead-Time Slip causing stockout gap inside 30-day window
    elif (float(sku.get("on_hand_units", 0)) / max(0.1, daily_units)) < float(sku.get("current_vendor_lead_days", 0)):
        days_cover = float(sku["on_hand_units"]) / max(0.1, daily_units)
        eff_lead = float(sku["current_vendor_lead_days"])
        stockout_days = min(30.0, max(0.0, eff_lead - days_cover))
        if stockout_days >= 1.5:
            anomaly_type = "vendor_lead_time_slip"
            lost_units = daily_units * stockout_days
            revenue_at_risk_30d = round(lost_units * price, 2)
            margin_at_risk_30d = round(lost_units * unit_margin, 2)
            recommended_specialist = "InventoryReplenishmentAgent"
            recommended_action = "EXECUTE_LATERAL_TRANSFER"
            summary = (
                f"{sku['primary_dc']} holds {days_cover:.1f}d cover ({sku['on_hand_units']} units) while vendor lead time "
                f"slipped to {eff_lead:.0f}d, creating a {stockout_days:.1f}d stockout window (${margin_at_risk_30d:,.2f} margin at risk)."
            )

    # 3. NCX Customer Return-Rate Spike (>= 9.0% and >= 2.2x baseline)
    if anomaly_type is None and float(sku.get("return_rate_pct", 0.0)) >= 9.0:
        cur_ret = float(sku["return_rate_pct"]) / 100.0
        base_ret = float(sku.get("baseline_return_rate_pct", 3.0)) / 100.0
        excess_ret = max(0.0, cur_ret - base_ret)
        monthly_units = daily_units * 30.0
        excess_returned_units = monthly_units * excess_ret
        per_return_loss = (
            float(sku.get("return_processing_fee_usd", 4.0))
            + float(sku.get("unsellable_return_fraction", 0.35)) * float(sku["unit_cogs_usd"])
            + unit_margin
        )
        anomaly_type = "ncx_return_spike"
        revenue_at_risk_30d = round(excess_returned_units * price, 2)
        margin_at_risk_30d = round(excess_returned_units * per_return_loss, 2)
        recommended_specialist = "ReturnsQualityAgent"
        recommended_action = "QUARANTINE_LOT_AND_PATCH_LISTING"
        summary = (
            f"NCX return rate jumped from {base_ret*100:.1f}% to {cur_ret*100:.1f}% ({sku.get('top_ncx_reason')}), "
            f"bleeding ${margin_at_risk_30d:,.2f} in 30d refunds, FBA return fees, and unsellable write-offs."
        )

    # 4. Aged FBA 181-365+ Day Inventory Surcharge Cliff
    if anomaly_type is None and (
        int(sku.get("aged_181_270_units", 0)) + int(sku.get("aged_271_plus_units", 0))
    ) >= 60:
        u_181 = int(sku.get("aged_181_270_units", 0))
        u_271 = int(sku.get("aged_271_plus_units", 0))
        cu_ft = float(sku.get("cubic_feet_per_unit", 0.5))
        # Amazon FBA aged surcharge schedule approximation: $1.50/cu ft (181-270d) and $5.45/cu ft (271-365d)
        surcharge_181 = u_181 * max(0.15, cu_ft * 1.50)
        surcharge_271 = u_271 * max(0.30, cu_ft * 5.45)
        monthly_carry = (u_181 + u_271) * float(sku.get("monthly_storage_fee_usd", 1.20))
        surcharge_exposure_30d = round(surcharge_181 + surcharge_271 + monthly_carry, 2)
        anomaly_type = "aged_fba_surcharge"
        revenue_at_risk_30d = round((u_181 + u_271) * price * 0.15, 2)
        margin_at_risk_30d = surcharge_exposure_30d
        recommended_specialist = "FBAEconomicsAgent"
        recommended_action = "CREATE_FBA_REMOVAL_ORDER"
        summary = (
            f"{u_181 + u_271} units ({cu_ft:.2f} cu ft/unit) hit FBA 181-365d Aged Inventory Surcharge assessment "
            f"in {sku.get('days_to_surcharge_cliff', 5)}d (${surcharge_exposure_30d:,.2f} 30d surcharge + storage burn)."
        )

    # 5. Featured Offer / Buy Box Loss
    if anomaly_type is None and (
        float(sku.get("baseline_buy_box_pct", 92.0)) - float(sku.get("buy_box_win_pct", 92.0))
    ) >= 14.0:
        bb_drop = (float(sku["baseline_buy_box_pct"]) - float(sku["buy_box_win_pct"])) / 100.0
        # Losing Buy Box reduces conversion velocity ~78% on lost share
        lost_units_30d = daily_units * 30.0 * bb_drop * 0.78
        anomaly_type = "buy_box_loss"
        revenue_at_risk_30d = round(lost_units_30d * price, 2)
        margin_at_risk_30d = round(lost_units_30d * unit_margin, 2)
        recommended_specialist = "FBAEconomicsAgent"
        recommended_action = "UPDATE_PRICE"
        match_econ = compute_unit_economics(sku, override_price_usd=float(sku["competitor_price_usd"]))
        summary = (
            f"Buy Box win rate fell from {sku['baseline_buy_box_pct']:.0f}% to {sku['buy_box_win_pct']:.0f}% "
            f"(competitor at ${sku['competitor_price_usd']:.2f} vs. ${price:.2f}); matching preserves "
            f"{match_econ['net_margin_pct']:.1f}% net margin."
        )

    if anomaly_type is None:
        return None

    total_dollar_impact_30d = round(margin_at_risk_30d, 2)
    return {
        "sku": sku["sku"],
        "asin": sku["asin"],
        "title": sku["title"],
        "category": sku["category"],
        "primary_dc": sku["primary_dc"],
        "anomaly_type": anomaly_type,
        "raw_alert_count": int(sku.get("raw_alert_count", 1)),
        "sale_price_usd": price,
        "competitor_price_usd": float(sku.get("competitor_price_usd", price)),
        "net_unit_margin_usd": econ["net_unit_margin_usd"],
        "net_margin_pct": econ["net_margin_pct"],
        "daily_demand_mean": daily_units,
        "on_hand_units": int(sku.get("on_hand_units", 0)),
        "revenue_at_risk_30d_usd": revenue_at_risk_30d,
        "margin_at_risk_30d_usd": margin_at_risk_30d,
        "surcharge_exposure_30d_usd": surcharge_exposure_30d,
        "total_dollar_impact_30d_usd": total_dollar_impact_30d,
        "recommended_specialist": recommended_specialist,
        "recommended_action": recommended_action,
        "min_margin_floor_pct": float(sku.get("min_margin_floor_pct", 15.0)),
        "summary": summary,
    }


def scan_anomalies(
    catalog: List[Dict[str, Any]],
    category_filter: Optional[str] = None,
    dc_filter: Optional[str] = None,
    anomaly_filter: Optional[str] = None,
    top_k: int = 15,
) -> Dict[str, Any]:
    """Scan all SKUs, rank strictly by 30-day dollar margin exposure, and benchmark vs. alert-count sorting."""
    t0 = time.perf_counter()

    filtered = [
        s
        for s in catalog
        if (not category_filter or category_filter == "ALL" or s["category"] == category_filter)
        and (not dc_filter or dc_filter == "ALL" or s["primary_dc"] == dc_filter)
    ]

    detected: List[Dict[str, Any]] = []
    for sku in filtered:
        finding = evaluate_sku_anomaly(sku)
        if finding is not None:
            if not anomaly_filter or anomaly_filter == "ALL" or finding["anomaly_type"] == anomaly_filter:
                detected.append(finding)

    # Strict expected 30-day dollar margin-at-risk ranking
    dollar_ranked = sorted(
        detected,
        key=lambda x: (x["total_dollar_impact_30d_usd"], x["revenue_at_risk_30d_usd"]),
        reverse=True,
    )
    for rank_idx, item in enumerate(dollar_ranked, start=1):
        item["dollar_rank"] = rank_idx

    # Naive alert-count sorting (how legacy seller/ops dashboards sort noisy notifications)
    naive_alert_ranked = sorted(
        detected,
        key=lambda x: (x["raw_alert_count"], x["sku"]),
        reverse=True,
    )
    naive_rank_map = {item["sku"]: idx for idx, item in enumerate(naive_alert_ranked, start=1)}
    for item in dollar_ranked:
        item["naive_alert_rank"] = naive_rank_map[item["sku"]]

    total_margin_exposure = round(sum(x["total_dollar_impact_30d_usd"] for x in dollar_ranked), 2)
    total_revenue_exposure = round(sum(x["revenue_at_risk_30d_usd"] for x in dollar_ranked), 2)

    top10_dollar_sum = sum(x["total_dollar_impact_30d_usd"] for x in dollar_ranked[:10])
    top10_naive_sum = sum(x["total_dollar_impact_30d_usd"] for x in naive_alert_ranked[:10])

    dollar_top10_capture_pct = round((top10_dollar_sum / total_margin_exposure) * 100.0, 2) if total_margin_exposure > 0 else 100.0
    naive_top10_capture_pct = round((top10_naive_sum / total_margin_exposure) * 100.0, 2) if total_margin_exposure > 0 else 0.0

    by_class: Dict[str, Dict[str, Any]] = {}
    for item in dollar_ranked:
        cls = item["anomaly_type"]
        bucket = by_class.setdefault(cls, {"count": 0, "margin_at_risk_30d_usd": 0.0, "revenue_at_risk_30d_usd": 0.0})
        bucket["count"] += 1
        bucket["margin_at_risk_30d_usd"] = round(bucket["margin_at_risk_30d_usd"] + item["total_dollar_impact_30d_usd"], 2)
        bucket["revenue_at_risk_30d_usd"] = round(bucket["revenue_at_risk_30d_usd"] + item["revenue_at_risk_30d_usd"], 2)

    scan_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    return {
        "scan_ms": scan_ms,
        "skus_scanned": len(filtered),
        "anomalies_detected": len(dollar_ranked),
        "total_margin_at_risk_30d_usd": total_margin_exposure,
        "total_revenue_at_risk_30d_usd": total_revenue_exposure,
        "ranking_comparison": {
            "dollar_ranked_top10_margin_usd": round(top10_dollar_sum, 2),
            "naive_alert_top10_margin_usd": round(top10_naive_sum, 2),
            "dollar_ranked_top10_capture_pct": dollar_top10_capture_pct,
            "naive_alert_top10_capture_pct": naive_top10_capture_pct,
            "triage_lift_pct_points": round(dollar_top10_capture_pct - naive_top10_capture_pct, 2),
        },
        "breakdown_by_anomaly_type": by_class,
        "ranked_anomalies": dollar_ranked[: max(1, int(top_k))],
    }
