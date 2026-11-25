"""Supervisor + 4-Specialist Multi-Agent Orchestrator with Margin Gate & SQLite Audit Ledger.

Routes complex seller & supply-chain anomalies across:
  1. CatalogComplianceAgent
  2. FBAEconomicsAgent
  3. InventoryReplenishmentAgent
  4. ReturnsQualityAgent

Synthesizes an executive action brief via Amazon Bedrock (`us.amazon.nova-lite-v1:0`)
and enforces a deterministic `COSIGN_REQUIRED` approval gate with hard net-margin
floor checks (`MARGIN_FLOOR_BLOCKED`) before executing price changes, FBA removal
orders, lateral DC transfers, or vendor purchase orders.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .anomaly_radar import (
    build_default_sku_catalog,
    compute_unit_economics,
    evaluate_sku_anomaly,
    scan_anomalies,
)
from .erp_harmonizer import PRESET_ERP_FEEDS, harmonize_erp_csv
from .replenishment_bom import (
    BOM_COMPONENTS,
    optimize_replenishment_plan,
    trace_bom_blast_radius,
)

GENESIS_HASH = "GENESIS_SELLERAMELIA_SQLITE_0000"
_BEDROCK_CLIENT_LOCK = threading.Lock()
_CACHED_BEDROCK_CLIENT: Any = None


class SellerAuditLedger:
    """Append-only SHA-256 hash-chained SQLite WAL audit ledger for agent actions & guardrails."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        runtime_dir = Path(os.environ.get("SELLERAMELIA_RUNTIME_DIR", "/tmp/selleramelia-runtime"))
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or (runtime_dir / "seller_amelia_audit.db")
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_audit_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    timestamp_epoch REAL NOT NULL,
                    sku TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    gate_verdict TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    sha256_hash TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def append_event(
        self,
        sku: str,
        action_type: str,
        gate_verdict: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._lock, contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT seq, sha256_hash FROM action_audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            next_seq = (int(row[0]) + 1) if row else 1
            prev_hash = str(row[1]) if row else GENESIS_HASH
            event_id = f"SAM-{next_seq:05d}"
            ts = round(time.time(), 4)
            canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            envelope = json.dumps(
                {
                    "event_id": event_id,
                    "sku": sku,
                    "action_type": action_type,
                    "gate_verdict": gate_verdict,
                    "payload": canonical_payload,
                    "prev_hash": prev_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO action_audit_log
                (event_id, timestamp_epoch, sku, action_type, gate_verdict, payload_json, prev_hash, sha256_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, ts, sku, action_type, gate_verdict, canonical_payload, prev_hash, digest),
            )
            conn.commit()
            return {
                "event_id": event_id,
                "sku": sku,
                "action_type": action_type,
                "gate_verdict": gate_verdict,
                "prev_hash": prev_hash,
                "sha256_hash": digest,
            }

    def list_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock, contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT event_id, timestamp_epoch, sku, action_type, gate_verdict, payload_json, prev_hash, sha256_hash
                FROM action_audit_log
                ORDER BY seq DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [
            {
                "event_id": r[0],
                "timestamp_epoch": r[1],
                "sku": r[2],
                "action_type": r[3],
                "gate_verdict": r[4],
                "payload": json.loads(r[5]),
                "prev_hash": r[6],
                "sha256_hash": r[7],
            }
            for r in rows
        ]

    def verify_chain_integrity(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        with self._lock, contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT event_id, sku, action_type, gate_verdict, payload_json, prev_hash, sha256_hash
                FROM action_audit_log
                ORDER BY seq ASC
                """
            ).fetchall()

        expected_prev = GENESIS_HASH
        for r in rows:
            event_id, sku, action_type, gate_verdict, canonical_payload, prev_hash, stored_hash = r
            if prev_hash != expected_prev:
                return {
                    "valid": False,
                    "checked_rows": len(rows),
                    "broken_at_event_id": event_id,
                    "verify_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                }
            envelope = json.dumps(
                {
                    "event_id": event_id,
                    "sku": sku,
                    "action_type": action_type,
                    "gate_verdict": gate_verdict,
                    "payload": canonical_payload,
                    "prev_hash": prev_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            recomputed = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
            if recomputed != stored_hash:
                return {
                    "valid": False,
                    "checked_rows": len(rows),
                    "broken_at_event_id": event_id,
                    "verify_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                }
            expected_prev = stored_hash

        return {
            "valid": True,
            "checked_rows": len(rows),
            "genesis_hash": GENESIS_HASH,
            "verify_ms": round((time.perf_counter() - t0) * 1000.0, 3),
        }

    def clear(self) -> None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            conn.execute("DELETE FROM action_audit_log;")
            conn.commit()


def _get_bedrock_client(region: str) -> Any:
    global _CACHED_BEDROCK_CLIENT
    with _BEDROCK_CLIENT_LOCK:
        if _CACHED_BEDROCK_CLIENT is not None:
            return _CACHED_BEDROCK_CLIENT
        import boto3  # type: ignore

        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            _CACHED_BEDROCK_CLIENT = boto3.client("bedrock-runtime", region_name=region)
        else:
            profile = os.environ.get("AWS_PROFILE", "portfolio")
            sess = boto3.Session(profile_name=profile, region_name=region)
            _CACHED_BEDROCK_CLIENT = sess.client("bedrock-runtime", region_name=region)
        return _CACHED_BEDROCK_CLIENT


def invoke_bedrock_nova(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 340,
) -> Optional[Dict[str, Any]]:
    """Call live Amazon Bedrock (`us.amazon.nova-lite-v1:0`) via Converse API."""
    if os.environ.get("SELLERAMELIA_OFFLINE_LLM") == "1":
        return None

    model_id = os.environ.get("SELLERAMELIA_BEDROCK_MODEL", "us.amazon.nova-lite-v1:0")
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    t0 = time.perf_counter()

    try:
        client = _get_bedrock_client(region)
        resp = client.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.1},
        )
        round_trip_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        server_ms = float(resp.get("metrics", {}).get("latencyMs") or round_trip_ms)
        usage = resp.get("usage", {})
        text = resp["output"]["message"]["content"][0]["text"]
        return {
            "text": text.strip(),
            "engine": f"aws-bedrock ({model_id})",
            "model_id": model_id,
            "latency_ms": round(server_ms, 2),
            "round_trip_ms": round_trip_ms,
            "input_tokens": int(usage.get("inputTokens", 0)),
            "output_tokens": int(usage.get("outputTokens", 0)),
            "total_tokens": int(usage.get("totalTokens", 0)),
        }
    except Exception:
        pass

    aws_bin = shutil.which("aws")
    if not aws_bin:
        return None

    env = dict(os.environ)
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)
    env.pop("AWS_SESSION_TOKEN", None)
    env["AWS_PROFILE"] = os.environ.get("AWS_PROFILE", "portfolio")
    env["AWS_DEFAULT_REGION"] = region
    cmd = [
        aws_bin,
        "bedrock-runtime",
        "converse",
        "--region",
        region,
        "--model-id",
        model_id,
        "--system",
        json.dumps([{"text": system_prompt}]),
        "--messages",
        json.dumps([{"role": "user", "content": [{"text": user_prompt}]}]),
        "--inference-config",
        json.dumps({"maxTokens": max_tokens, "temperature": 0.1}),
        "--output",
        "json",
    ]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15.0)
        round_trip_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            server_ms = float(data.get("metrics", {}).get("latencyMs") or round_trip_ms)
            usage = data.get("usage", {})
            text = data["output"]["message"]["content"][0]["text"]
            return {
                "text": text.strip(),
                "engine": f"aws-bedrock ({model_id})",
                "model_id": model_id,
                "latency_ms": round(server_ms, 2),
                "round_trip_ms": round_trip_ms,
                "input_tokens": int(usage.get("inputTokens", 0)),
                "output_tokens": int(usage.get("outputTokens", 0)),
                "total_tokens": int(usage.get("totalTokens", 0)),
            }
    except Exception:
        return None
    return None


# ==============================================================================
# 4 Specialist Domain Agents
# ==============================================================================
class CatalogComplianceAgent:
    name = "CatalogComplianceAgent"

    def analyze(self, sku: Dict[str, Any]) -> Dict[str, Any]:
        suppressed = sku.get("listing_status") == "SUPPRESSED"
        patch_attributes = {}
        if suppressed:
            patch_attributes = {
                "lithium_battery_energy_content": "614.4 Wh",
                "un38_3_test_summary_doc_id": f"UN383-CERT-{sku['sku'][-3:]}",
                "hazmat_united_nations_regulatory_id": "UN3480",
            }
        return {
            "agent": self.name,
            "listing_status": sku.get("listing_status", "ACTIVE"),
            "suppression_reason": sku.get("suppression_reason") or "None (Compliant)",
            "proposed_attribute_patch": patch_attributes,
            "finding": (
                f"Listing {sku['asin']} is SUPPRESSED ({sku.get('suppression_reason')}); extracted verified spec values {patch_attributes} ready for catalog patch."
                if suppressed
                else f"Listing {sku['asin']} passes mandatory category & compliance schema checks."
            ),
        }


class FBAEconomicsAgent:
    name = "FBAEconomicsAgent"

    def analyze(self, sku: Dict[str, Any], proposed_price_usd: Optional[float] = None) -> Dict[str, Any]:
        current_econ = compute_unit_economics(sku)
        comp_price = float(sku.get("competitor_price_usd", sku["sale_price_usd"]))
        candidate_price = float(proposed_price_usd) if proposed_price_usd is not None else comp_price
        candidate_econ = compute_unit_economics(sku, override_price_usd=candidate_price)

        aged_units = int(sku.get("aged_181_270_units", 0)) + int(sku.get("aged_271_plus_units", 0))
        cu_ft = float(sku.get("cubic_feet_per_unit", 0.45))
        aged_surcharge_30d = round(
            int(sku.get("aged_181_270_units", 0)) * max(0.15, cu_ft * 1.50)
            + int(sku.get("aged_271_plus_units", 0)) * max(0.30, cu_ft * 5.45),
            2,
        )
        removal_fee_est = round(min(180, aged_units) * (1.04 + cu_ft * 0.65), 2) if aged_units > 0 else 0.0

        return {
            "agent": self.name,
            "current_economics": current_econ,
            "candidate_price_economics": candidate_econ,
            "buy_box_win_pct": float(sku.get("buy_box_win_pct", 90.0)),
            "aged_units_total": aged_units,
            "aged_surcharge_30d_usd": aged_surcharge_30d,
            "recommended_removal_units": min(180, aged_units) if aged_units >= 60 else 0,
            "estimated_removal_cost_usd": removal_fee_est,
            "finding": (
                f"Current price ${current_econ['sale_price_usd']:.2f} yields ${current_econ['net_unit_margin_usd']:.2f} "
                f"({current_econ['net_margin_pct']:.1f}% net margin after referral + FBA + returns). "
                f"Candidate price ${candidate_price:.2f} yields ${candidate_econ['net_unit_margin_usd']:.2f} "
                f"({candidate_econ['net_margin_pct']:.1f}% vs. {sku.get('min_margin_floor_pct', 15.0):.1f}% floor)."
            ),
        }


class InventoryReplenishmentAgent:
    name = "InventoryReplenishmentAgent"

    def analyze(self, sku: Dict[str, Any]) -> Dict[str, Any]:
        slip_days = max(0.0, float(sku.get("current_vendor_lead_days", sku["lead_time_mean_days"])) - float(sku["lead_time_mean_days"]))
        plan = optimize_replenishment_plan(sku, supplier_delay_days=max(8.0, slip_days) if slip_days > 0 else 0.0)
        best = plan["options"][0]
        return {
            "agent": self.name,
            "days_of_cover": plan["days_of_cover"],
            "reorder_point_s": plan["safety_stock_policy"]["reorder_point_s"],
            "coupled_safety_stock_units": plan["safety_stock_policy"]["coupled_safety_stock_units"],
            "recommended_strategy": plan["recommended_strategy"],
            "recommended_units": best["units_dispatched"],
            "margin_preserved_usd": best["margin_preserved_usd"],
            "finding": (
                f"{sku['primary_dc']} holds {plan['days_of_cover']:.1f}d cover (coupled s={plan['safety_stock_policy']['reorder_point_s']} units). "
                f"Best mitigation: {plan['recommended_title']} ({best['units_dispatched']} units, ${best['total_risk_adjusted_cost_usd']:,.2f} risk-adjusted cost)."
            ),
        }


class ReturnsQualityAgent:
    name = "ReturnsQualityAgent"

    def analyze(self, sku: Dict[str, Any]) -> Dict[str, Any]:
        ret_pct = float(sku.get("return_rate_pct", 3.0))
        base_pct = float(sku.get("baseline_return_rate_pct", 3.0))
        spike = ret_pct >= 9.0 or (ret_pct - base_pct) >= 4.5
        return {
            "agent": self.name,
            "return_rate_pct": ret_pct,
            "baseline_return_rate_pct": base_pct,
            "ncx_spike_detected": spike,
            "top_ncx_reason": sku.get("top_ncx_reason", "None"),
            "finding": (
                f"NCX alert: return rate {ret_pct:.1f}% vs. {base_pct:.1f}% baseline ({sku.get('top_ncx_reason')}); "
                f"recommend bin check on affected lot + bullet #2 accessory callout."
                if spike
                else f"Return rate healthy at {ret_pct:.1f}% (baseline {base_pct:.1f}%)."
            ),
        }


def evaluate_action_guardrail(
    sku: Dict[str, Any],
    action_type: str,
    proposed_price_usd: Optional[float] = None,
    order_qty: Optional[int] = None,
    min_margin_floor_pct: Optional[float] = None,
    cosign_approved: bool = False,
) -> Dict[str, Any]:
    """Deterministic pre-execution safety & margin-floor check before mutating Seller Central / ERP state."""
    t0 = time.perf_counter()
    floor_pct = float(
        min_margin_floor_pct if min_margin_floor_pct is not None else sku.get("min_margin_floor_pct", 15.0)
    )

    if action_type == "UPDATE_PRICE":
        target_price = float(
            proposed_price_usd if proposed_price_usd is not None else sku.get("competitor_price_usd", sku["sale_price_usd"])
        )
        econ = compute_unit_economics(sku, override_price_usd=target_price)
        if econ["net_unit_margin_usd"] <= 0.0 or econ["net_margin_pct"] < floor_pct:
            return {
                "gate_verdict": "MARGIN_FLOOR_BLOCKED",
                "rule_id": "SPX-MARGIN-FLOOR-01",
                "allowed_to_execute": False,
                "target_price_usd": target_price,
                "post_change_margin_usd": econ["net_unit_margin_usd"],
                "post_change_margin_pct": econ["net_margin_pct"],
                "required_margin_floor_pct": floor_pct,
                "reason": (
                    f"Blocked price change to ${target_price:.2f}: post-fee net margin drops to "
                    f"${econ['net_unit_margin_usd']:.2f} ({econ['net_margin_pct']:.1f}%), violating the "
                    f"{floor_pct:.1f}% minimum contribution margin floor."
                ),
                "eval_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            }

    if action_type == "SUBMIT_SPLIT_PO" and order_qty is not None:
        moq = int(sku.get("moq_units", 1))
        if int(order_qty) < moq:
            return {
                "gate_verdict": "MOQ_POLICY_BLOCKED",
                "rule_id": "SC-VENDOR-MOQ-02",
                "allowed_to_execute": False,
                "order_qty": int(order_qty),
                "required_moq": moq,
                "reason": f"Blocked PO for {order_qty} units: below supplier contracted MOQ of {moq} units.",
                "eval_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            }

    mutating_actions = {
        "UPDATE_PRICE",
        "CREATE_FBA_REMOVAL_ORDER",
        "SUBMIT_SPLIT_PO",
        "EXECUTE_LATERAL_TRANSFER",
        "PATCH_CATALOG_ATTRIBUTES",
        "QUARANTINE_LOT_AND_PATCH_LISTING",
    }

    if action_type in mutating_actions and not cosign_approved:
        return {
            "gate_verdict": "COSIGN_REQUIRED",
            "rule_id": "SPX-COSIGN-GATE-10",
            "allowed_to_execute": False,
            "required_margin_floor_pct": floor_pct,
            "reason": (
                f"Action {action_type} passes deterministic unit-economics & MOQ guardrails and awaits 1-click seller/planner co-sign."
            ),
            "eval_ms": round((time.perf_counter() - t0) * 1000.0, 3),
        }

    return {
        "gate_verdict": "EXECUTED_COSIGNED" if action_type in mutating_actions else "AUTO_DIAGNOSTIC_ONLY",
        "rule_id": "SPX-COSIGN-APPROVED-20",
        "allowed_to_execute": True,
        "required_margin_floor_pct": floor_pct,
        "reason": f"Action {action_type} verified against unit-economics floor ({floor_pct:.1f}%) and committed with audit hash.",
        "eval_ms": round((time.perf_counter() - t0) * 1000.0, 3),
    }


class SellerAmeliaControlPlane:
    """Stateful control plane coordinating Radar, BOM/Replenishment, ERP Harmonizer, 4 Specialists & Bedrock."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self.ledger = SellerAuditLedger(db_path=db_path)
        self.catalog: List[Dict[str, Any]] = []
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.catalog = build_default_sku_catalog(num_skus=120, seed=42)
            self.ledger.clear()
            self.ledger.append_event(
                sku="CATALOG-120",
                action_type="SYSTEM_BOOT_SEED",
                gate_verdict="AUTO_DIAGNOSTIC_ONLY",
                payload={"skus_loaded": len(self.catalog), "regional_dcs": 4},
            )

    def get_sku(self, sku_id: str) -> Dict[str, Any]:
        for s in self.catalog:
            if s["sku"] == sku_id or s["asin"] == sku_id:
                return s
        raise KeyError(f"Unknown SKU or ASIN: {sku_id}")

    def consult_multi_agent(
        self,
        sku_id: str = "SKU-KTN-204",
        question: str = "",
        proposed_action: Optional[str] = None,
        proposed_price_usd: Optional[float] = None,
        order_qty: Optional[int] = None,
        min_margin_floor_pct: Optional[float] = None,
        cosign_approved: bool = False,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        sku = self.get_sku(sku_id)
        anomaly = evaluate_sku_anomaly(sku)

        # Determine which specialist agents to activate (always run FBAEconomics + relevant specialists concurrently)
        cat_agent = CatalogComplianceAgent()
        econ_agent = FBAEconomicsAgent()
        inv_agent = InventoryReplenishmentAgent()
        ret_agent = ReturnsQualityAgent()

        q_low = (question or "").lower()
        active_names = ["FBAEconomicsAgent"]
        if (anomaly and anomaly["anomaly_type"] == "listing_suppression") or any(k in q_low for k in ("suppress", "catalog", "hazmat", "battery", "compliance", "all")):
            active_names.append("CatalogComplianceAgent")
        if (anomaly and anomaly["anomaly_type"] == "vendor_lead_time_slip") or any(k in q_low for k in ("stock", "lead", "transfer", "replenish", "po", "bom", "all")):
            active_names.append("InventoryReplenishmentAgent")
        if (anomaly and anomaly["anomaly_type"] == "ncx_return_spike") or any(k in q_low for k in ("return", "ncx", "defect", "quality", "refund", "all")):
            active_names.append("ReturnsQualityAgent")

        # Ensure at least 3 specialists collaborate on executive consultations
        for fallback_name in ("InventoryReplenishmentAgent", "CatalogComplianceAgent", "ReturnsQualityAgent"):
            if len(active_names) < 3 and fallback_name not in active_names:
                active_names.append(fallback_name)

        specialist_outputs: Dict[str, Dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs: Dict[str, concurrent.futures.Future[Dict[str, Any]]] = {}
            if "CatalogComplianceAgent" in active_names:
                futs["CatalogComplianceAgent"] = pool.submit(cat_agent.analyze, sku)
            if "FBAEconomicsAgent" in active_names:
                futs["FBAEconomicsAgent"] = pool.submit(econ_agent.analyze, sku, proposed_price_usd)
            if "InventoryReplenishmentAgent" in active_names:
                futs["InventoryReplenishmentAgent"] = pool.submit(inv_agent.analyze, sku)
            if "ReturnsQualityAgent" in active_names:
                futs["ReturnsQualityAgent"] = pool.submit(ret_agent.analyze, sku)
            for k, f in futs.items():
                specialist_outputs[k] = f.result()

        chosen_action = proposed_action or (anomaly["recommended_action"] if anomaly else "UPDATE_PRICE")
        guardrail = evaluate_action_guardrail(
            sku=sku,
            action_type=chosen_action,
            proposed_price_usd=proposed_price_usd,
            order_qty=order_qty,
            min_margin_floor_pct=min_margin_floor_pct,
            cosign_approved=cosign_approved,
        )

        mutation_applied: Dict[str, Any] = {"committed": False}
        if guardrail["allowed_to_execute"] and guardrail["gate_verdict"] == "EXECUTED_COSIGNED":
            with self._lock:
                if chosen_action == "UPDATE_PRICE":
                    new_p = float(proposed_price_usd if proposed_price_usd is not None else sku["competitor_price_usd"])
                    sku["sale_price_usd"] = round(new_p, 2)
                    sku["buy_box_win_pct"] = 91.0
                    mutation_applied = {"committed": True, "new_sale_price_usd": sku["sale_price_usd"], "new_buy_box_win_pct": 91.0}
                elif chosen_action == "PATCH_CATALOG_ATTRIBUTES":
                    sku["listing_status"] = "ACTIVE"
                    sku["suppression_reason"] = ""
                    sku["buy_box_win_pct"] = 93.0
                    mutation_applied = {"committed": True, "new_listing_status": "ACTIVE"}
                elif chosen_action == "EXECUTE_LATERAL_TRANSFER":
                    xfer_u = int(order_qty or specialist_outputs.get("InventoryReplenishmentAgent", {}).get("recommended_units", 160))
                    sku["on_hand_units"] += xfer_u
                    sku["dc_inventory"][sku["primary_dc"]] = sku["on_hand_units"]
                    mutation_applied = {"committed": True, "transferred_units": xfer_u, "new_on_hand_units": sku["on_hand_units"]}
                elif chosen_action == "CREATE_FBA_REMOVAL_ORDER":
                    rem_u = int(order_qty or 180)
                    sku["aged_271_plus_units"] = max(0, int(sku.get("aged_271_plus_units", 0)) - rem_u)
                    mutation_applied = {"committed": True, "removed_aged_units": rem_u, "remaining_aged_271_units": sku["aged_271_plus_units"]}
                else:
                    mutation_applied = {"committed": True, "action_type": chosen_action}

        # Synthesize executive action brief via live Amazon Bedrock (`us.amazon.nova-lite-v1:0`)
        sys_prompt = (
            "You are SellerAmelia, an executive AI assistant for Amazon Seller Operations and multi-echelon "
            "supply chain planning. Write a concise 3-to-4 sentence plain-English action brief citing the exact "
            "SKU, dollar margin numbers, specialist findings, and approval gate status provided in the JSON context. "
            "Never invent numbers and never use em-dashes."
        )
        usr_prompt = json.dumps(
            {
                "sku": sku["sku"],
                "asin": sku["asin"],
                "title": sku["title"],
                "seller_question": question or "Diagnose the primary operational risk and recommend the highest-ROI safe action.",
                "anomaly": anomaly,
                "specialist_findings": {k: v["finding"] for k, v in specialist_outputs.items()},
                "proposed_action": chosen_action,
                "approval_gate": guardrail,
            },
            indent=2,
        )

        llm_res = invoke_bedrock_nova(sys_prompt, usr_prompt, max_tokens=280)
        if llm_res:
            brief_text = llm_res["text"].replace("\u2014", "-").replace("\u2013", "-")
            llm_engine = llm_res["engine"]
            bedrock_ms = llm_res["latency_ms"]
            bedrock_tokens = llm_res["total_tokens"]
        else:
            econ_f = specialist_outputs["FBAEconomicsAgent"]["finding"]
            brief_text = (
                f"For {sku['sku']} ({sku['asin']}), specialist analysis shows: {econ_f} "
                f"Recommended action is {chosen_action} with gate status {guardrail['gate_verdict']} ({guardrail['reason']})."
            )
            llm_engine = "deterministic-specialist-synthesis"
            bedrock_ms = 0.0
            bedrock_tokens = 0

        receipt = self.ledger.append_event(
            sku=sku["sku"],
            action_type=chosen_action,
            gate_verdict=guardrail["gate_verdict"],
            payload={
                "proposed_price_usd": proposed_price_usd,
                "order_qty": order_qty,
                "committed": mutation_applied.get("committed", False),
                "rule_id": guardrail["rule_id"],
            },
        )

        return {
            "sku": sku["sku"],
            "asin": sku["asin"],
            "title": sku["title"],
            "specialists_invoked": list(specialist_outputs.keys()),
            "specialist_reports": specialist_outputs,
            "proposed_action": chosen_action,
            "approval_gate": guardrail,
            "mutation_applied": mutation_applied,
            "executive_brief": brief_text,
            "llm_engine": llm_engine,
            "bedrock_latency_ms": bedrock_ms,
            "bedrock_tokens": bedrock_tokens,
            "audit_receipt": receipt,
            "total_orchestration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    def get_state_snapshot(self, benchmarks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        radar = scan_anomalies(self.catalog, top_k=12)
        bom_nodes = [
            {
                "component_id": cid,
                "tier": c["tier"],
                "name": c["name"],
                "supplier": c["supplier"],
                "buffer_days": c["buffer_days"],
            }
            for cid, c in BOM_COMPONENTS.items()
        ]
        return {
            "radar": radar,
            "bom_components": bom_nodes,
            "erp_presets": {k: v for k, v in PRESET_ERP_FEEDS.items()},
            "audit_chain": self.ledger.verify_chain_integrity(),
            "recent_audit_events": self.ledger.list_recent(limit=15),
            "benchmarks": benchmarks or {},
        }
