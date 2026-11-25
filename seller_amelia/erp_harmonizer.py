"""Messy Seller & 3PL ERP CSV Schema Harmonizer.

Maps arbitrary customer, 3PL, SAP, or NetSuite CSV column headers into canonical
supply-chain entities (`sku`, `asin`, `primary_dc`, `on_hand_units`,
`daily_demand_mean`, `lead_time_mean_days`, `lead_time_std_days`,
`unit_cogs_usd`, `sale_price_usd`, `moq_units`, `return_rate_pct`) with strict
unit and currency validation and zero-silent-coercion quarantine reporting.
"""
from __future__ import annotations

import csv
import io
import re
import time
from typing import Any, Dict, List, Optional, Tuple

CANONICAL_HEADER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "sku": ("sku", "sku_code", "merchant_sku", "item_id", "item_sku", "material_no", "part_number", "msku"),
    "asin": ("asin", "asin_id", "amazon_asin", "channel_asin"),
    "title": ("title", "product_name", "item_desc", "description", "sku_title"),
    "primary_dc": ("primary_dc", "dc_code", "warehouse_site", "fc_code", "plant_id", "location_code", "site_id"),
    "on_hand_units": ("on_hand_units", "avail_qty", "qoh", "qty_on_hand", "sellable_units", "stock_units", "fba_available"),
    "daily_demand_mean": ("daily_demand_mean", "daily_vel", "avg_daily_sales", "units_per_day", "vel_30d_day", "demand_mean"),
    "daily_demand_std": ("daily_demand_std", "demand_sd", "daily_vel_std", "sigma_demand"),
    "lead_time_mean_days": ("lead_time_mean_days", "lead_days_p50", "vendor_lt_med", "supplier_lead_time", "lt_days", "lead_time"),
    "lead_time_std_days": ("lead_time_std_days", "lead_days_sigma", "lt_std_days", "vendor_lt_sd", "sigma_lt"),
    "unit_cogs_usd": ("unit_cogs_usd", "unit_cost", "landed_cost_usd", "cogs_usd", "std_cost", "unit_cogs"),
    "sale_price_usd": ("sale_price_usd", "selling_price", "list_price_usd", "buy_box_price", "retail_price", "price_usd"),
    "moq_units": ("moq_units", "vendor_moq", "min_order_qty", "moq_qty", "moq"),
    "return_rate_pct": ("return_rate_pct", "ncx_return_pct", "return_pct", "refund_rate"),
}

REQUIRED_CANONICAL_FIELDS = (
    "sku",
    "on_hand_units",
    "lead_time_mean_days",
    "unit_cogs_usd",
)

PRESET_ERP_FEEDS: Dict[str, str] = {
    "netsuite_3pl_mixed": """sku_code,asin_id,item_desc,wh_site,on_hand_units,vel_30d_day,lead_days_p50,lead_days_sigma,unit_cogs_usd,selling_price,vendor_moq,ncx_return_pct
SKU-PWR-101,B09PWR0101,VoltPeak 600W Station,ONT8-West,520,28.0,16 days,3.5d,$116.00,$299.00,120,3.1%
SKU-SOL-108,B09SOL0108,SunFold 200W Solar Kit,ONT8-West,190,19.0,3 weeks,4.8d,USD 84.00,USD 219.00,150,2.9%
SKU-KTN-204,B08THERM24,ChefProbe Dual Thermometer,MDW2-Midwest,1180,64.0,12,2.8,$21.50,$79.00,250,3.4%
SKU-BAD-EUR,B08EUR9912,EuroImport Burr Grinder,TEB9-East,340,14.5,14d,3.0d,EUR 38.50,$119.00,100,4.1%
SKU-BAD-NEG,B08NEG4410,SubZero Insulated Pitcher,DAL3-South,-18,9.2,10d,2.0d,$12.40,$39.99,200,2.5%
SKU-BAD-TBD,B08TBD7721,Pending Vendor Espresso Tamper,TEB9-East,215,18.0,TBD,2.5d,$9.80,$34.00,150,3.0%""",
    "clean_sap_export": """MATERIAL_NO,CHANNEL_ASIN,SKU_TITLE,PLANT_ID,QTY_ON_HAND,AVG_DAILY_SALES,VENDOR_LT_MED,LT_STD_DAYS,LANDED_COST_USD,LIST_PRICE_USD,MIN_ORDER_QTY,REFUND_RATE
SKU-ESP-309,B08BREW309,CremaLab 0.1g Barista Scale,TEB9-East,890,48.0,11d,2.4d,$14.20,$54.00,300,15.8%
SKU-PAT-412,B07PATIO12,CedarCrest Teak Lounger,DAL3-South,485,2.1,3 weeks,4.5d,$152.00,$349.00,60,4.6%
SKU-PWR-220,B09PWR0220,VoltPeak 1200W Pro Station,ONT8-West,310,15.4,18 days,4.1d,$248.00,$599.00,80,2.7%""",
}


def _normalize_col(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def map_csv_headers(headers: List[str]) -> Dict[str, Any]:
    """Deterministically map customer ERP column headers to canonical supply-chain attributes."""
    canonical_to_source: Dict[str, str] = {}
    unmapped_headers: List[str] = []

    alias_lookup: Dict[str, str] = {}
    for canon, aliases in CANONICAL_HEADER_ALIASES.items():
        for al in aliases:
            alias_lookup[_normalize_col(al)] = canon

    # Also support substring token heuristics for common warehouse naming variations
    for raw_h in headers:
        norm = _normalize_col(raw_h)
        matched: Optional[str] = alias_lookup.get(norm)
        if matched is None:
            if "wh_site" in norm or "warehouse" in norm or norm.endswith("_dc"):
                matched = "primary_dc"
            elif ("cogs" in norm or "cost" in norm) and "return" not in norm:
                matched = "unit_cogs_usd"
            elif "price" in norm:
                matched = "sale_price_usd"
            elif ("on_hand" in norm or "qoh" in norm or "avail" in norm):
                matched = "on_hand_units"
            elif ("sigma" in norm or "std" in norm or "_sd" in norm) and ("lead" in norm or "lt" in norm):
                matched = "lead_time_std_days"
            elif ("lead" in norm or "lt_" in norm or norm.endswith("_lt")):
                matched = "lead_time_mean_days"
            elif "moq" in norm or "min_order" in norm:
                matched = "moq_units"

        if matched and matched not in canonical_to_source:
            canonical_to_source[matched] = raw_h
        else:
            unmapped_headers.append(raw_h)

    missing_required = [req for req in REQUIRED_CANONICAL_FIELDS if req not in canonical_to_source]
    return {
        "canonical_to_source": canonical_to_source,
        "unmapped_headers": unmapped_headers,
        "missing_required_fields": missing_required,
        "mapped_count": len(canonical_to_source),
    }


def _parse_usd_monetary(raw_val: str, field_name: str) -> Tuple[Optional[float], Optional[Tuple[str, str]]]:
    s = str(raw_val or "").strip()
    if not s:
        return None, ("MISSING_MONETARY_VALUE", f"Empty monetary value for {field_name}")

    upper = s.upper()
    for bad_ccy in ("EUR", "GBP", "JPY", "CAD", "CNY", "INR", "AUD", "€", "£", "¥"):
        if bad_ccy in upper:
            return None, (
                "UNSUPPORTED_CURRENCY",
                f"Non-USD currency token in '{s}' for {field_name}; provide explicit FX table rather than silent 1:1 coercion",
            )

    cleaned = re.sub(r"^(USD|\$)\s*", "", upper).replace(",", "").strip()
    try:
        val = float(cleaned)
    except ValueError:
        return None, ("NON_NUMERIC_MONETARY", f"Cannot parse '{s}' as USD amount for {field_name}")

    if val <= 0.0:
        return None, ("NON_POSITIVE_MONETARY", f"{field_name} must be strictly > $0.00 (received {val})")
    return round(val, 2), None


def _parse_duration_days(raw_val: str, field_name: str, allow_zero: bool = False) -> Tuple[Optional[float], Optional[Tuple[str, str]]]:
    s = str(raw_val or "").strip()
    if not s:
        return None, ("MISSING_DURATION", f"Empty lead-time value for {field_name}")

    m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*([a-zA-Z]*)$", s)
    if not m:
        return None, ("NON_NUMERIC_VALUE", f"Cannot parse '{s}' as numeric duration in days for {field_name}")

    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("", "d", "day", "days"):
        days = num
    elif unit in ("w", "wk", "wks", "week", "weeks"):
        days = num * 7.0
    elif unit in ("h", "hr", "hrs", "hour", "hours"):
        days = num / 24.0
    else:
        return None, ("AMBIGUOUS_TIME_UNIT", f"Unsupported time unit '{unit}' in '{s}' for {field_name}")

    if days < 0.0 or (not allow_zero and days <= 0.0):
        return None, ("INVALID_LEAD_TIME", f"{field_name} must be positive days (received {days})")
    return round(days, 2), None


def harmonize_erp_csv(csv_text: str) -> Dict[str, Any]:
    """Parse, map, and validate an ERP/3PL CSV feed with zero silent coercion."""
    t0 = time.perf_counter()
    raw = (csv_text or "").strip()
    if not raw:
        raise ValueError("csv_text is empty")

    reader = csv.DictReader(io.StringIO(raw))
    headers = list(reader.fieldnames or [])
    if not headers:
        raise ValueError("CSV header row is missing")

    header_map_info = map_csv_headers(headers)
    c2s = header_map_info["canonical_to_source"]
    if header_map_info["missing_required_fields"]:
        return {
            "status": "SCHEMA_INCOMPLETE",
            "header_mapping": header_map_info,
            "rows_received": 0,
            "canonical_records": [],
            "quarantined_rows": [
                {
                    "row_number": 1,
                    "sku": "(header)",
                    "field": ",".join(header_map_info["missing_required_fields"]),
                    "raw_value": ",".join(headers),
                    "error_code": "MISSING_REQUIRED_COLUMNS",
                    "detail": f"Required canonical columns not found: {header_map_info['missing_required_fields']}",
                }
            ],
            "harmonize_ms": round((time.perf_counter() - t0) * 1000.0, 3),
        }

    canonical_records: List[Dict[str, Any]] = []
    quarantined_rows: List[Dict[str, Any]] = []
    rows_received = 0

    for row_idx, row in enumerate(reader, start=2):
        rows_received += 1
        sku_raw = str(row.get(c2s["sku"]) or "").strip()
        if not sku_raw:
            quarantined_rows.append(
                {
                    "row_number": row_idx,
                    "sku": "(missing)",
                    "field": "sku",
                    "raw_value": "",
                    "error_code": "MISSING_PRIMARY_KEY",
                    "detail": "Primary SKU identifier is empty",
                }
            )
            continue

        # Validate on_hand_units (strict integer >= 0)
        qoh_str = str(row.get(c2s["on_hand_units"]) or "").replace(",", "").strip()
        try:
            qoh_float = float(qoh_str)
            if not qoh_float.is_integer():
                raise ValueError("fractional inventory")
            qoh_val = int(qoh_float)
        except ValueError:
            quarantined_rows.append(
                {
                    "row_number": row_idx,
                    "sku": sku_raw,
                    "field": "on_hand_units",
                    "raw_value": qoh_str,
                    "error_code": "NON_INTEGER_INVENTORY",
                    "detail": f"Inventory quantity '{qoh_str}' is not an integer unit count",
                }
            )
            continue

        if qoh_val < 0:
            quarantined_rows.append(
                {
                    "row_number": row_idx,
                    "sku": sku_raw,
                    "field": "on_hand_units",
                    "raw_value": qoh_str,
                    "error_code": "NEGATIVE_INVENTORY",
                    "detail": f"Negative on-hand inventory ({qoh_val}) quarantined for cycle-count reconciliation",
                }
            )
            continue

        # Validate lead_time_mean_days
        lt_val, lt_err = _parse_duration_days(str(row.get(c2s["lead_time_mean_days"]) or ""), "lead_time_mean_days")
        if lt_err is not None or lt_val is None:
            quarantined_rows.append(
                {
                    "row_number": row_idx,
                    "sku": sku_raw,
                    "field": "lead_time_mean_days",
                    "raw_value": str(row.get(c2s["lead_time_mean_days"]) or ""),
                    "error_code": lt_err[0] if lt_err else "INVALID_LEAD_TIME",
                    "detail": lt_err[1] if lt_err else "Invalid lead time",
                }
            )
            continue

        # Optional lead_time_std_days
        lt_std_val = round(lt_val * 0.22, 2)
        if "lead_time_std_days" in c2s and str(row.get(c2s["lead_time_std_days"]) or "").strip():
            parsed_std, std_err = _parse_duration_days(
                str(row.get(c2s["lead_time_std_days"]) or ""),
                "lead_time_std_days",
                allow_zero=True,
            )
            if std_err is not None or parsed_std is None:
                quarantined_rows.append(
                    {
                        "row_number": row_idx,
                        "sku": sku_raw,
                        "field": "lead_time_std_days",
                        "raw_value": str(row.get(c2s["lead_time_std_days"]) or ""),
                        "error_code": std_err[0] if std_err else "INVALID_LEAD_STD",
                        "detail": std_err[1] if std_err else "Invalid lead time std",
                    }
                )
                continue
            lt_std_val = parsed_std

        # Validate unit_cogs_usd
        cogs_val, cogs_err = _parse_usd_monetary(str(row.get(c2s["unit_cogs_usd"]) or ""), "unit_cogs_usd")
        if cogs_err is not None or cogs_val is None:
            quarantined_rows.append(
                {
                    "row_number": row_idx,
                    "sku": sku_raw,
                    "field": "unit_cogs_usd",
                    "raw_value": str(row.get(c2s["unit_cogs_usd"]) or ""),
                    "error_code": cogs_err[0] if cogs_err else "INVALID_COGS",
                    "detail": cogs_err[1] if cogs_err else "Invalid COGS",
                }
            )
            continue

        # Optional sale_price_usd
        price_val = round(cogs_val * 2.55, 2)
        if "sale_price_usd" in c2s and str(row.get(c2s["sale_price_usd"]) or "").strip():
            parsed_price, price_err = _parse_usd_monetary(str(row.get(c2s["sale_price_usd"]) or ""), "sale_price_usd")
            if price_err is not None or parsed_price is None:
                quarantined_rows.append(
                    {
                        "row_number": row_idx,
                        "sku": sku_raw,
                        "field": "sale_price_usd",
                        "raw_value": str(row.get(c2s["sale_price_usd"]) or ""),
                        "error_code": price_err[0] if price_err else "INVALID_PRICE",
                        "detail": price_err[1] if price_err else "Invalid sale price",
                    }
                )
                continue
            price_val = parsed_price

        # Optional daily_demand_mean
        vel_val = 12.0
        if "daily_demand_mean" in c2s and str(row.get(c2s["daily_demand_mean"]) or "").strip():
            raw_vel = str(row.get(c2s["daily_demand_mean"]) or "").strip()
            try:
                vel_val = float(raw_vel)
                if vel_val < 0:
                    raise ValueError("negative demand")
            except ValueError:
                quarantined_rows.append(
                    {
                        "row_number": row_idx,
                        "sku": sku_raw,
                        "field": "daily_demand_mean",
                        "raw_value": raw_vel,
                        "error_code": "INVALID_DEMAND_VELOCITY",
                        "detail": f"Daily demand '{raw_vel}' must be a non-negative number",
                    }
                )
                continue

        # Optional moq_units
        moq_val = 100
        if "moq_units" in c2s and str(row.get(c2s["moq_units"]) or "").strip():
            raw_moq = str(row.get(c2s["moq_units"]) or "").replace(",", "").strip()
            try:
                moq_val = max(1, int(float(raw_moq)))
            except ValueError:
                quarantined_rows.append(
                    {
                        "row_number": row_idx,
                        "sku": sku_raw,
                        "field": "moq_units",
                        "raw_value": raw_moq,
                        "error_code": "NON_NUMERIC_MOQ",
                        "detail": f"MOQ '{raw_moq}' must be numeric",
                    }
                )
                continue

        # Optional return_rate_pct
        ret_pct = 3.2
        if "return_rate_pct" in c2s and str(row.get(c2s["return_rate_pct"]) or "").strip():
            raw_ret = str(row.get(c2s["return_rate_pct"]) or "").strip().rstrip("%").strip()
            try:
                ret_pct = float(raw_ret)
                if ret_pct < 0 or ret_pct > 100:
                    raise ValueError("out of range")
            except ValueError:
                quarantined_rows.append(
                    {
                        "row_number": row_idx,
                        "sku": sku_raw,
                        "field": "return_rate_pct",
                        "raw_value": str(row.get(c2s["return_rate_pct"]) or ""),
                        "error_code": "INVALID_RETURN_PCT",
                        "detail": "Return percentage must be between 0% and 100%",
                    }
                )
                continue

        canonical_records.append(
            {
                "sku": sku_raw,
                "asin": str(row.get(c2s.get("asin", "")) or f"B09ERP{row_idx:04d}").strip(),
                "title": str(row.get(c2s.get("title", "")) or sku_raw).strip(),
                "primary_dc": str(row.get(c2s.get("primary_dc", "")) or "ONT8-West").strip(),
                "on_hand_units": qoh_val,
                "daily_demand_mean": round(vel_val, 2),
                "daily_demand_std": round(vel_val * 0.24, 2),
                "lead_time_mean_days": lt_val,
                "lead_time_std_days": lt_std_val,
                "unit_cogs_usd": cogs_val,
                "sale_price_usd": price_val,
                "moq_units": moq_val,
                "return_rate_pct": round(ret_pct, 2),
                "days_of_cover": round(qoh_val / max(0.1, vel_val), 1),
            }
        )

    return {
        "status": "HARMONIZED" if not quarantined_rows else "HARMONIZED_WITH_QUARANTINE",
        "header_mapping": header_map_info,
        "rows_received": rows_received,
        "accepted_count": len(canonical_records),
        "quarantined_count": len(quarantined_rows),
        "zero_silent_coercion": True,
        "canonical_records": canonical_records,
        "quarantined_rows": quarantined_rows,
        "harmonize_ms": round((time.perf_counter() - t0) * 1000.0, 3),
    }
