"""HTTP Server & AWS CloudFront `/api/*` Lambda Origin Handler for SellerAmelia."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .anomaly_radar import scan_anomalies
from .erp_harmonizer import PRESET_ERP_FEEDS, harmonize_erp_csv
from .replenishment_bom import optimize_replenishment_plan, trace_bom_blast_radius
from .specialist_agents import SellerAmeliaControlPlane, evaluate_action_guardrail

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DOCS_DIR = ROOT_DIR / "docs"
BENCHMARK_JSON_PATH = ROOT_DIR / "benchmarks" / "latest_results.json"

CONTROL_PLANE = SellerAmeliaControlPlane()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def _load_benchmarks() -> Dict[str, Any]:
    if BENCHMARK_JSON_PATH.is_file():
        try:
            return json.loads(BENCHMARK_JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def route_api_request(
    method: str,
    path: str,
    query: Dict[str, str],
    body: Dict[str, Any],
    request_id: str = "local",
) -> Tuple[int, Dict[str, Any]]:
    """Pure router shared by local HTTP server and AWS Lambda `/api/*` origin behind CloudFront."""
    try:
        if method == "GET" and path == "/api/health":
            chain = CONTROL_PLANE.ledger.verify_chain_integrity()
            return 200, {
                "status": "ok",
                "service": "seller-amelia-spx",
                "delivery": os.environ.get("SELLERAMELIA_DELIVERY", "cloudfront-s3-oac + lambda-api-origin"),
                "aws_region": os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2")),
                "bedrock_model": os.environ.get("SELLERAMELIA_BEDROCK_MODEL", "us.amazon.nova-lite-v1:0"),
                "python_version": platform.python_version(),
                "skus_monitored": len(CONTROL_PLANE.catalog),
                "sqlite_audit_chain_valid": chain["valid"],
                "sqlite_audit_rows": chain["checked_rows"],
                "request_id": request_id,
            }

        if method == "GET" and path == "/api/state":
            snap = CONTROL_PLANE.get_state_snapshot(benchmarks=_load_benchmarks())
            snap["request_id"] = request_id
            return 200, snap

        if method == "GET" and path == "/api/benchmarks":
            data = _load_benchmarks()
            return 200, {"status": "ok", "benchmarks": data, "request_id": request_id}

        if path == "/api/radar/scan" and method in ("GET", "POST"):
            params = {**query, **body}
            cat = str(params.get("category") or "ALL").strip()
            dc = str(params.get("dc") or "ALL").strip()
            anom = str(params.get("anomaly_type") or "ALL").strip()
            top_k = int(params.get("top_k") or 15)
            out = scan_anomalies(
                CONTROL_PLANE.catalog,
                category_filter=cat,
                dc_filter=dc,
                anomaly_filter=anom,
                top_k=top_k,
            )
            out["request_id"] = request_id
            return 200, out

        if method == "POST" and path == "/api/replenish/optimize":
            sku_id = str(body.get("sku") or "SKU-SOL-108").strip()
            component_id = str(body.get("component_id") or "COMP-BMS-IC04").strip()
            delay_days = float(body.get("supplier_delay_days", 12.0))
            service_level = float(body.get("service_level", 0.975))
            deficit_dc = body.get("deficit_dc")

            sku = CONTROL_PLANE.get_sku(sku_id)
            replenishment = optimize_replenishment_plan(
                sku=sku,
                supplier_delay_days=delay_days,
                service_level=service_level,
                deficit_dc=str(deficit_dc).strip() if deficit_dc else None,
            )
            bom_blast = trace_bom_blast_radius(
                component_id=component_id,
                supplier_delay_days=delay_days,
                sku_catalog=CONTROL_PLANE.catalog,
            )
            CONTROL_PLANE.ledger.append_event(
                sku=sku["sku"],
                action_type="REPLENISH_BOM_SIMULATION",
                gate_verdict="AUTO_DIAGNOSTIC_ONLY",
                payload={
                    "component_id": component_id,
                    "supplier_delay_days": delay_days,
                    "recommended_strategy": replenishment["recommended_strategy"],
                },
            )
            return 200, {
                "replenishment": replenishment,
                "bom_blast_radius": bom_blast,
                "request_id": request_id,
            }

        if method == "POST" and path == "/api/agents/consult":
            sku_id = str(body.get("sku") or "SKU-KTN-204").strip()
            question = str(body.get("question") or "").strip()
            proposed_action = body.get("proposed_action")
            raw_price = body.get("proposed_price_usd")
            proposed_price = float(raw_price) if raw_price not in (None, "") else None
            raw_qty = body.get("order_qty")
            order_qty = int(raw_qty) if raw_qty not in (None, "") else None
            raw_floor = body.get("min_margin_floor_pct")
            min_floor = float(raw_floor) if raw_floor not in (None, "") else None
            cosign = bool(body.get("cosign_approved", False))

            out = CONTROL_PLANE.consult_multi_agent(
                sku_id=sku_id,
                question=question,
                proposed_action=str(proposed_action).strip() if proposed_action else None,
                proposed_price_usd=proposed_price,
                order_qty=order_qty,
                min_margin_floor_pct=min_floor,
                cosign_approved=cosign,
            )
            out["request_id"] = request_id
            return 200, out

        if method == "POST" and path == "/api/erp/ingest":
            preset_name = body.get("preset")
            csv_text = str(body.get("csv_text") or "").strip()
            if not csv_text and preset_name in PRESET_ERP_FEEDS:
                csv_text = PRESET_ERP_FEEDS[str(preset_name)]
            if not csv_text:
                return 400, {"error": "csv_text or valid preset is required"}

            harm = harmonize_erp_csv(csv_text)

            # Optional margin guardrail test on a candidate price override in the same workflow
            guardrail_preview = None
            if body.get("test_sku") or body.get("test_price_usd") not in (None, ""):
                target_sku_id = str(body.get("test_sku") or "SKU-KTN-204").strip()
                target_sku = CONTROL_PLANE.get_sku(target_sku_id)
                test_price = float(body.get("test_price_usd", target_sku["competitor_price_usd"]))
                floor_pct = float(body.get("min_margin_floor_pct", target_sku.get("min_margin_floor_pct", 18.0)))
                cosign = bool(body.get("cosign_approved", False))
                guardrail_preview = evaluate_action_guardrail(
                    sku=target_sku,
                    action_type="UPDATE_PRICE",
                    proposed_price_usd=test_price,
                    min_margin_floor_pct=floor_pct,
                    cosign_approved=cosign,
                )
                CONTROL_PLANE.ledger.append_event(
                    sku=target_sku["sku"],
                    action_type="ERP_INGEST_AND_MARGIN_CHECK",
                    gate_verdict=guardrail_preview["gate_verdict"],
                    payload={
                        "accepted_rows": harm.get("accepted_count", 0),
                        "quarantined_rows": harm.get("quarantined_count", 0),
                        "test_price_usd": test_price,
                    },
                )
            else:
                CONTROL_PLANE.ledger.append_event(
                    sku="ERP-BATCH",
                    action_type="ERP_CSV_HARMONIZE",
                    gate_verdict="AUTO_DIAGNOSTIC_ONLY",
                    payload={
                        "accepted_rows": harm.get("accepted_count", 0),
                        "quarantined_rows": harm.get("quarantined_count", 0),
                    },
                )

            harm["margin_gate_check"] = guardrail_preview
            harm["audit_chain"] = CONTROL_PLANE.ledger.verify_chain_integrity()
            harm["request_id"] = request_id
            return 200, harm

        if method == "POST" and path == "/api/reset":
            CONTROL_PLANE.reset()
            return 200, {
                "status": "reset_complete",
                "service": "seller-amelia-spx",
                "skus_monitored": len(CONTROL_PLANE.catalog),
                "request_id": request_id,
            }

        return 404, {"error": f"Unknown API route: {method} {path}"}
    except KeyError as exc:
        return 404, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:
        return 500, {"error": str(exc)}


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """AWS Lambda Function URL handler mounted behind Amazon CloudFront `/api/*` origin."""
    req_ctx = event.get("requestContext") or {}
    method = ((req_ctx.get("http") or {}).get("method") or event.get("httpMethod") or "GET").upper()
    raw_path = event.get("rawPath") or event.get("path") or "/"
    query_params = {k: str(v) for k, v in (event.get("queryStringParameters") or {}).items() if v is not None}
    req_id = getattr(context, "aws_request_id", "cloudfront-api")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if raw_path.startswith("/api/"):
        body: Dict[str, Any] = {}
        raw_body = event.get("body")
        if raw_body:
            if event.get("isBase64Encoded") and isinstance(raw_body, str):
                raw_body = base64.b64decode(raw_body).decode("utf-8")
            body = json.loads(raw_body) if isinstance(raw_body, str) else dict(raw_body)
        status, payload = route_api_request(method, raw_path, query_params, body, request_id=req_id)
        return {
            "statusCode": status,
            "headers": {
                **CORS_HEADERS,
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
            },
            "body": json.dumps(payload, default=str),
        }

    target = WEB_DIR / ("index.html" if raw_path in ("/", "") else raw_path.lstrip("/"))
    if not target.is_file() and raw_path.endswith(".svg"):
        target = DOCS_DIR / Path(raw_path).name
    if target.is_file():
        ctype = mimetypes.guess_type(str(target))[0] or "text/html; charset=utf-8"
        return {
            "statusCode": 200,
            "headers": {**CORS_HEADERS, "Content-Type": ctype},
            "body": target.read_text(encoding="utf-8"),
        }
    return {
        "statusCode": 404,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": "Not found", "path": raw_path}),
    }


class LocalSellerAmeliaHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}

        if path.startswith("/api/"):
            status, payload = route_api_request("GET", path, query, {}, request_id="local-dev")
            raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = (WEB_DIR / rel).resolve()
        if not candidate.is_file() and path.endswith(".svg"):
            candidate = (DOCS_DIR / Path(path).name).resolve()
        if not candidate.is_file():
            candidate = WEB_DIR / "index.html"

        data = candidate.read_bytes() if candidate.is_file() else b"<h1>SellerAmelia</h1>"
        ctype = mimetypes.guess_type(str(candidate))[0] or "text/html"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        body = json.loads(raw_body) if raw_body.strip() else {}
        status, payload = route_api_request("POST", parsed.path, {}, body, request_id="local-dev")
        raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run_local_server(port: int = 3099) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), LocalSellerAmeliaHandler)
    print(f"SellerAmelia Studio running at http://localhost:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    port_num = int(os.environ.get("PORT", "3099"))
    run_local_server(port=port_num)
