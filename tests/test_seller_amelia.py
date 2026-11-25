"""Comprehensive unit tests for SellerAmelia core modules and API router."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["SELLERAMELIA_OFFLINE_LLM"] = "1"

from seller_amelia.anomaly_radar import (
    build_default_sku_catalog,
    compute_unit_economics,
    scan_anomalies,
)
from seller_amelia.erp_harmonizer import PRESET_ERP_FEEDS, harmonize_erp_csv
from seller_amelia.replenishment_bom import (
    compute_coupled_ss_policy,
    optimize_replenishment_plan,
    run_monte_carlo_policy_comparison,
    trace_bom_blast_radius,
)
from seller_amelia.server import lambda_handler
from seller_amelia.specialist_agents import (
    SellerAmeliaControlPlane,
    SellerAuditLedger,
    evaluate_action_guardrail,
)


class TestSellerAmelia(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_default_sku_catalog(num_skus=120, seed=42)

    def test_01_catalog_and_dollar_impact_ranking_beats_alert_count(self) -> None:
        res = scan_anomalies(self.catalog, top_k=25)
        self.assertEqual(res["skus_scanned"], 120)
        self.assertGreaterEqual(res["anomalies_detected"], 20)
        ranked = res["ranked_anomalies"]
        # Strictly non-increasing by 30-day dollar impact
        for i in range(len(ranked) - 1):
            self.assertGreaterEqual(
                ranked[i]["total_dollar_impact_30d_usd"],
                ranked[i + 1]["total_dollar_impact_30d_usd"],
            )
        # Dollar-impact ranking must capture meaningfully more 30d margin in top-10 than noisy alert-count sorting
        comp = res["ranking_comparison"]
        self.assertGreater(
            comp["dollar_ranked_top10_capture_pct"],
            comp["naive_alert_top10_capture_pct"] + 25.0,
        )

    def test_02_unit_economics_exact_fee_breakdown(self) -> None:
        sku = next(s for s in self.catalog if s["sku"] == "SKU-KTN-204")
        econ = compute_unit_economics(sku, override_price_usd=79.00)
        self.assertEqual(econ["referral_fee_usd"], 11.85)
        self.assertEqual(econ["fba_fulfillment_fee_usd"], 5.35)
        self.assertGreater(econ["net_unit_margin_usd"], 35.0)
        self.assertGreater(econ["net_margin_pct"], 45.0)

    def test_03_coupled_ss_safety_stock_math(self) -> None:
        pol = compute_coupled_ss_policy(
            daily_demand_mean=25.0,
            daily_demand_std=6.0,
            lead_time_mean_days=14.0,
            lead_time_std_days=4.5,
            unit_cogs_usd=50.0,
            service_level=0.975,
            moq_units=120,
        )
        self.assertGreater(pol["coupled_safety_stock_units"], pol["naive_safety_stock_units"])
        self.assertEqual(
            pol["reorder_point_s"],
            int(25.0 * 14.0) + pol["coupled_safety_stock_units"],
        )
        self.assertGreaterEqual(pol["order_up_to_S"] - pol["reorder_point_s"], 120)

    def test_04_ntier_bom_delay_blast_radius(self) -> None:
        blast = trace_bom_blast_radius(
            component_id="COMP-BMS-IC04",
            supplier_delay_days=15.0,
            sku_catalog=self.catalog,
        )
        self.assertEqual(blast["tier"], 2)
        self.assertAlmostEqual(blast["net_propagated_delay_days"], 12.0, places=2)
        self.assertGreaterEqual(blast["impacted_finished_skus_count"], 3)
        impacted_ids = {x["sku"] for x in blast["impacted_asins"]}
        self.assertIn("SKU-PWR-101", impacted_ids)
        self.assertIn("SKU-SOL-108", impacted_ids)
        self.assertGreater(blast["total_margin_at_risk_30d_usd"], 15000.0)

    def test_05_lateral_transfer_vs_split_po_vs_air_optimizer(self) -> None:
        sku = next(s for s in self.catalog if s["sku"] == "SKU-SOL-108")
        plan = optimize_replenishment_plan(sku, supplier_delay_days=13.0, service_level=0.975)
        self.assertEqual(plan["recommended_strategy"], "LATERAL_DC_TRANSFER")
        self.assertEqual(len(plan["options"]), 3)
        by_id = {o["strategy_id"]: o for o in plan["options"]}
        self.assertLess(
            by_id["LATERAL_DC_TRANSFER"]["total_risk_adjusted_cost_usd"],
            by_id["EXPEDITED_AIR_FREIGHT"]["total_risk_adjusted_cost_usd"],
        )
        self.assertGreaterEqual(by_id["SPLIT_VENDOR_PO"]["units_dispatched"], sku["moq_units"])

    def test_06_monte_carlo_coupled_policy_reduces_stockouts(self) -> None:
        mc = run_monte_carlo_policy_comparison(self.catalog, num_trials=40, horizon_days=45, seed=2026)
        self.assertLess(
            mc["coupled_ss_plus_lateral"]["stockout_day_rate_pct"],
            mc["naive_fixed_reorder"]["stockout_day_rate_pct"],
        )
        self.assertGreater(
            mc["coupled_ss_plus_lateral"]["unit_fill_rate_pct"],
            mc["naive_fixed_reorder"]["unit_fill_rate_pct"],
        )
        self.assertGreater(mc["cost_savings_per_trial_usd"], 0.0)

    def test_07_erp_csv_harmonizer_maps_messy_headers_and_units(self) -> None:
        res = harmonize_erp_csv(PRESET_ERP_FEEDS["clean_sap_export"])
        self.assertEqual(res["status"], "HARMONIZED")
        self.assertEqual(res["accepted_count"], 3)
        self.assertEqual(res["quarantined_count"], 0)
        patio = next(r for r in res["canonical_records"] if r["sku"] == "SKU-PAT-412")
        # "3 weeks" converted deterministically to 21.0 days
        self.assertEqual(patio["lead_time_mean_days"], 21.0)
        self.assertEqual(patio["unit_cogs_usd"], 152.0)

    def test_08_erp_harmonizer_zero_silent_coercion_quarantines_errors(self) -> None:
        res = harmonize_erp_csv(PRESET_ERP_FEEDS["netsuite_3pl_mixed"])
        self.assertEqual(res["status"], "HARMONIZED_WITH_QUARANTINE")
        self.assertEqual(res["accepted_count"], 3)
        self.assertEqual(res["quarantined_count"], 3)
        codes = {q["error_code"] for q in res["quarantined_rows"]}
        self.assertEqual(codes, {"UNSUPPORTED_CURRENCY", "NEGATIVE_INVENTORY", "NON_NUMERIC_VALUE"})

    def test_09_margin_floor_guardrail_blocks_loss_making_reprice(self) -> None:
        sku = next(s for s in self.catalog if s["sku"] == "SKU-KTN-204")
        # Even with cosign_approved=True, loss-making / below-floor price must be hard-blocked
        blocked = evaluate_action_guardrail(
            sku=sku,
            action_type="UPDATE_PRICE",
            proposed_price_usd=29.99,
            min_margin_floor_pct=18.0,
            cosign_approved=True,
        )
        self.assertEqual(blocked["gate_verdict"], "MARGIN_FLOOR_BLOCKED")
        self.assertFalse(blocked["allowed_to_execute"])

        # Sub-MOQ purchase order must also be blocked even with co-sign
        moq_blocked = evaluate_action_guardrail(
            sku=sku,
            action_type="SUBMIT_SPLIT_PO",
            order_qty=40,
            cosign_approved=True,
        )
        self.assertEqual(moq_blocked["gate_verdict"], "MOQ_POLICY_BLOCKED")
        self.assertFalse(moq_blocked["allowed_to_execute"])

    def test_10_cosign_gate_and_state_mutation_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = SellerAmeliaControlPlane(db_path=Path(tmp) / "audit.db")
            # Without co-sign, action pauses at COSIGN_REQUIRED
            pending = cp.consult_multi_agent(
                sku_id="SKU-KTN-204",
                proposed_action="UPDATE_PRICE",
                proposed_price_usd=75.50,
                cosign_approved=False,
            )
            self.assertEqual(pending["approval_gate"]["gate_verdict"], "COSIGN_REQUIRED")
            self.assertFalse(pending["mutation_applied"]["committed"])

            # With co-sign, valid price update executes and updates live catalog state
            approved = cp.consult_multi_agent(
                sku_id="SKU-KTN-204",
                proposed_action="UPDATE_PRICE",
                proposed_price_usd=75.50,
                cosign_approved=True,
            )
            self.assertEqual(approved["approval_gate"]["gate_verdict"], "EXECUTED_COSIGNED")
            self.assertTrue(approved["mutation_applied"]["committed"])
            self.assertEqual(cp.get_sku("SKU-KTN-204")["sale_price_usd"], 75.50)

    def test_11_sqlite_wal_hash_chain_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "chain.db"
            ledger = SellerAuditLedger(db_path=db_file)
            ledger.append_event("SKU-PWR-101", "PATCH_CATALOG_ATTRIBUTES", "COSIGN_REQUIRED", {"step": 1})
            ledger.append_event("SKU-PWR-101", "PATCH_CATALOG_ATTRIBUTES", "EXECUTED_COSIGNED", {"step": 2})
            self.assertTrue(ledger.verify_chain_integrity()["valid"])

            # Tamper directly with row 1 in SQLite
            conn = sqlite3.connect(str(db_file))
            conn.execute("UPDATE action_audit_log SET gate_verdict='AUTO_DIAGNOSTIC_ONLY' WHERE seq=1")
            conn.commit()
            conn.close()

            tampered_check = ledger.verify_chain_integrity()
            self.assertFalse(tampered_check["valid"])
            self.assertEqual(tampered_check["broken_at_event_id"], "SAM-00001")

    def test_12_lambda_handler_api_routes(self) -> None:
        h_resp = lambda_handler({"rawPath": "/api/health", "requestContext": {"http": {"method": "GET"}}})
        self.assertEqual(h_resp["statusCode"], 200)
        self.assertEqual(json.loads(h_resp["body"])["status"], "ok")

        r_resp = lambda_handler(
            {
                "rawPath": "/api/radar/scan",
                "requestContext": {"http": {"method": "POST"}},
                "body": json.dumps({"top_k": 5}),
            }
        )
        self.assertEqual(r_resp["statusCode"], 200)
        self.assertEqual(len(json.loads(r_resp["body"])["ranked_anomalies"]), 5)

        opt_resp = lambda_handler(
            {
                "rawPath": "/api/replenish/optimize",
                "requestContext": {"http": {"method": "POST"}},
                "body": json.dumps({"sku": "SKU-SOL-108", "component_id": "COMP-BMS-IC04", "supplier_delay_days": 14}),
            }
        )
        self.assertEqual(opt_resp["statusCode"], 200)
        self.assertIn("replenishment", json.loads(opt_resp["body"]))

        erp_resp = lambda_handler(
            {
                "rawPath": "/api/erp/ingest",
                "requestContext": {"http": {"method": "POST"}},
                "body": json.dumps({"preset": "netsuite_3pl_mixed", "test_sku": "SKU-KTN-204", "test_price_usd": 31.0}),
            }
        )
        self.assertEqual(erp_resp["statusCode"], 200)
        erp_payload = json.loads(erp_resp["body"])
        self.assertEqual(erp_payload["quarantined_count"], 3)
        self.assertEqual(erp_payload["margin_gate_check"]["gate_verdict"], "MARGIN_FLOOR_BLOCKED")


if __name__ == "__main__":
    unittest.main()
