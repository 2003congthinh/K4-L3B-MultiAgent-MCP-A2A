"""Evidence-first L3B workflow.

The workflow is intentionally deterministic: MCP evidence establishes facts and
claim outcomes. No external LLM is called during evaluation.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .mcp_gateway import EvidenceGateway


# The starter gateway targets an older MCP SDK attribute name. Keep the fix in
# the only editable Python artifact instead of changing mcp_gateway.py.
async def _gateway_call_compat(
    self: EvidenceGateway, tool_name: str, *, case_id: str, **arguments: str
) -> dict[str, object]:
    payload = {"case_id": case_id, **arguments}
    result = await self._session.call_tool(tool_name, arguments=payload)
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", False)
    if is_error:
        message = " ".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
    evidence = getattr(result, "structured_content", None)
    if evidence is None:
        evidence = getattr(result, "structuredContent", None)
    if evidence is None:
        text_blocks = [
            block.text for block in result.content if getattr(block, "text", None)
        ]
        if len(text_blocks) != 1:
            raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
        evidence = json.loads(text_blocks[0])
    self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
    return evidence


EvidenceGateway.call = _gateway_call_compat


class ToolCache:
    def __init__(self) -> None:
        self._store: dict[tuple, dict] = {}

    def key(self, tool_name: str, kwargs: dict) -> tuple:
        return tool_name, tuple(sorted((k, repr(v)) for k, v in kwargs.items()))

    def get(self, tool_name: str, kwargs: dict) -> Optional[dict]:
        return self._store.get(self.key(tool_name, kwargs))

    def put(self, tool_name: str, kwargs: dict, evidence: dict) -> None:
        self._store[self.key(tool_name, kwargs)] = evidence


async def call_tool(
    gateway, trace, cache: ToolCache, *, case_id: str, actor: str,
    tool_name: str, expected_domain: str | None = None, **kwargs: Any,
) -> dict:
    cached = cache.get(tool_name, kwargs)
    if cached is not None:
        return cached
    evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
    if expected_domain and evidence.get("domain") != expected_domain:
        raise ValueError(
            f"{tool_name} returned domain={evidence.get('domain')!r}; expected {expected_domain!r}"
        )
    trace.emit(
        case_id=case_id, event_type="tool_result_consumed", actor=actor,
        tool_name=tool_name, evidence_refs=[evidence["evidence_ref"]],
    )
    cache.put(tool_name, kwargs, evidence)
    return evidence


async def optional_tool(
    gateway, trace, cache: ToolCache, *, case_id: str, actor: str,
    tool_name: str, expected_domain: str | None = None, **kwargs: Any,
) -> dict | None:
    """Best-effort evidence lookup.

    This is deliberately used for refund lookup. A missing refund record is a
    normal state, not an error requiring retry. Other optional evidence is also
    allowed to be absent without fabricating a result.
    """
    try:
        return await call_tool(
            gateway, trace, cache, case_id=case_id, actor=actor,
            tool_name=tool_name, expected_domain=expected_domain, **kwargs,
        )
    except (RuntimeError, ValueError):
        return None


@dataclass
class Claim:
    field: str
    value: Any
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)
    source_agent: str = ""


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _values(data: Any, *names: str) -> list[Any]:
    wanted = {n.lower() for n in names}
    out: list[Any] = []
    for obj in _walk(data):
        for key, value in obj.items():
            if key.lower() in wanted and value not in (None, "", []):
                out.append(value)
    return out


def _first(data: Any, *names: str, default: Any = None) -> Any:
    values = _values(data, *names)
    return values[0] if values else default


def _strings(data: Any, *names: str) -> list[str]:
    out: list[str] = []
    for value in _values(data, *names):
        if isinstance(value, (str, int, float)):
            value = str(value)
            if value and value not in out:
                out.append(value)
    return out


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value.replace(" ", ""))
        if match:
            try:
                return float(match.group(0).replace(",", "."))
            except ValueError:
                return None
    return None


def _sum_numbers(data: Any, names: tuple[str, ...]) -> float | None:
    nums = [_number(v) for v in _values(data, *names)]
    nums = [n for n in nums if n is not None]
    return round(sum(nums), 2) if nums else None


def _lower_text(data: Any) -> str:
    chunks: list[str] = []
    for obj in _walk(data):
        for value in obj.values():
            if isinstance(value, str):
                chunks.append(value.lower())
    return " ".join(chunks)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _iso_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _order_ids(data: Any) -> list[str]:
    return _strings(data, "order_id", "orderId", "id")


def _item_ids(data: Any) -> list[str]:
    return _strings(data, "item_id", "itemId", "order_item_id", "orderItemId")


def _seller_ids(data: Any) -> list[str]:
    return _strings(data, "seller_id", "sellerId", "seller_unique_id", "sellerUniqueId")


def _shipment_ids(data: Any) -> list[str]:
    return _strings(data, "shipment_id", "shipmentId", "shipping_id", "shippingId")


def _payment_refs(data: Any) -> list[str]:
    return _strings(
        data, "payment_reference", "payment_reference_id", "payment_id", "paymentId",
        "transaction_id", "transactionId",
    )


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

async def resolve_entity(case: dict, gateway, trace, cache: ToolCache) -> dict:
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    claimed = request.get("claimed_order_id")
    candidates = list(dict.fromkeys(case.get("candidate_order_ids", [])))

    # A claimed order id is authoritative only after get_order confirms the
    # returned identity. Do not silently substitute another candidate.
    if claimed:
        try:
            evidence = await call_tool(
                gateway, trace, cache, case_id=case_id, actor="entity-agent",
                tool_name="get_order", expected_domain="order", order_id=claimed,
            )
        except (RuntimeError, ValueError):
            evidence = None
        if evidence is not None:
            data = evidence["data"]
            actual = _first(data, "order_id", "orderId", default=claimed)
            if actual == claimed:
                return {
                    "status": "resolved", "resolved_order_ids": [claimed],
                    "rejected_candidates": [c for c in candidates if c != claimed],
                    "confidence": 1.0,
                    "customer_unique_id": _first(
                        data, "customer_unique_id", "customerUniqueId",
                        default=case.get("customer_unique_id_hint"),
                    ),
                    "evidence_refs": [evidence["evidence_ref"]], "order_data": data,
                }

    scored: list[dict] = []
    for candidate in candidates:
        try:
            evidence = await call_tool(
                gateway, trace, cache, case_id=case_id, actor="entity-agent",
                tool_name="get_order", expected_domain="order", order_id=candidate,
            )
        except (RuntimeError, ValueError):
            continue
        data = evidence["data"]
        scored.append({
            "order_id": candidate, "score": _score_candidate(case, data),
            "data": data, "evidence_ref": evidence["evidence_ref"],
        })

    if not scored:
        return _unresolved("not_found")
    scored.sort(key=lambda x: (-x["score"], x["order_id"]))
    best = scored[0]
    rest = scored[1:]
    if best["score"] < 0.60:
        return {
            "status": "not_found", "resolved_order_ids": [],
            "rejected_candidates": [x["order_id"] for x in scored],
            "confidence": round(best["score"], 2), "customer_unique_id": None,
            "evidence_refs": [x["evidence_ref"] for x in scored], "order_data": {},
        }
    if rest and best["score"] - rest[0]["score"] < 0.15:
        return {
            "status": "ambiguous", "resolved_order_ids": [],
            "rejected_candidates": [x["order_id"] for x in scored],
            "confidence": round(best["score"], 2), "customer_unique_id": None,
            "evidence_refs": [x["evidence_ref"] for x in scored], "order_data": {},
        }
    return {
        "status": "resolved", "resolved_order_ids": [best["order_id"]],
        "rejected_candidates": [x["order_id"] for x in rest],
        "confidence": round(best["score"], 2),
        "customer_unique_id": _first(
            best["data"], "customer_unique_id", "customerUniqueId",
            default=case.get("customer_unique_id_hint"),
        ),
        "evidence_refs": [best["evidence_ref"]], "order_data": best["data"],
    }


def _unresolved(status: str) -> dict:
    return {
        "status": status, "resolved_order_ids": [], "rejected_candidates": [],
        "confidence": 0.0, "customer_unique_id": None,
        "evidence_refs": [], "order_data": {},
    }


def _score_candidate(case: dict, order_data: dict) -> float:
    request = case.get("customer_request", {})
    claimed = request.get("claimed_order_id")
    ids = _order_ids(order_data)
    score = 0.0
    if claimed and claimed in ids:
        score += 0.75
    hint = case.get("customer_unique_id_hint")
    customer = _first(order_data, "customer_unique_id", "customerUniqueId")
    if hint and customer == hint:
        score += 0.20
    opened = _iso_date(case.get("opened_at"))
    order_date = _iso_date(_first(order_data, "order_date", "orderDate", "created_at", "createdAt"))
    if opened and order_date and abs((opened - order_date).days) <= 120:
        score += 0.05
    return min(score, 1.0)


# ---------------------------------------------------------------------------
# Specialist agents
# ---------------------------------------------------------------------------

async def order_agent(order_id: str, case_id: str, gateway, trace, cache) -> dict:
    evidence = await call_tool(
        gateway, trace, cache, case_id=case_id, actor="order-agent",
        tool_name="get_order_items", expected_domain="item", order_id=order_id,
    )
    data = evidence["data"]
    return {
        "data": data, "item_ids": _item_ids(data), "seller_ids": _seller_ids(data),
        "evidence_refs": [evidence["evidence_ref"]],
    }


async def shipment_agent(order_id: str, case_id: str, gateway, trace, cache) -> dict:
    evidence = await call_tool(
        gateway, trace, cache, case_id=case_id, actor="shipment-agent",
        tool_name="get_shipment_summary", expected_domain="shipment", order_id=order_id,
    )
    data = evidence["data"]
    text = _lower_text(data)
    statuses = [str(v).lower() for v in _values(data, "status", "shipment_status", "delivery_status")]
    verdict = "insufficient_evidence"
    if _has_any(text, ("lost", "missing", "extraviado")):
        verdict = "lost"
    elif _has_any(text, ("returned", "return", "devolvido")):
        verdict = "returned"
    elif _has_any(text, ("seller_delay", "seller late", "late seller", "seller delayed")):
        verdict = "seller_delay"
    elif _has_any(text, ("logistics_delay", "carrier delay", "logistics delayed", "transport")):
        verdict = "logistics_delay"
    elif _has_any(" ".join(statuses), ("delivered_on_time", "on_time", "ontime")):
        verdict = "on_time"
    elif _has_any(text, ("late", "delayed", "atras")):
        verdict = "conflicting" if _has_any(text, ("conflict", "contradict")) else "logistics_delay"
    return {
        "verdict": verdict,
        "late_seller_ids": _seller_ids(data) if verdict == "seller_delay" else [],
        "timeline_complete": bool(_values(data, "events", "timeline", "milestones", "delivered_at", "delivery_date")),
        "_evidence_refs": [evidence["evidence_ref"]],
        "_shipment_ids": _shipment_ids(data), "_data": data,
    }


async def payment_refund_agent(order_id: str, case: dict, case_id: str, gateway, trace, cache) -> dict:
    payments, timeline = await asyncio.gather(
        call_tool(
            gateway, trace, cache, case_id=case_id, actor="payment-agent",
            tool_name="get_order_payments", expected_domain="payment", order_id=order_id,
        ),
        call_tool(
            gateway, trace, cache, case_id=case_id, actor="payment-agent",
            tool_name="get_payment_timeline", expected_domain="payment", order_id=order_id,
        ),
    )
    pdata, tdata = payments["data"], timeline["data"]

    # A refund timeline is not a required lookup for an arbitrary order. In
    # particular, "requested_full_refund" is a requested action, not evidence
    # that a refund transaction exists. Only refund-state cases get this call.
    topics = {c.get("topic", "") for c in case.get("customer_request", {}).get("claims", [])}
    refund_relevant = bool(topics & {"refund_pending", "refund_failed"})
    payment_text = _lower_text({"payment": pdata, "timeline": tdata})
    refund_relevant = refund_relevant or _has_any(
        payment_text, ("refund pending", "pending refund", "refund failed", "refund_failure", "refunded")
    )

    refunds = None
    if refund_relevant:
        # A missing refund record is an expected state. Do not retry and do not
        # turn it into a workflow error.
        refunds = await optional_tool(
            gateway, trace, cache, case_id=case_id, actor="refund-agent",
            tool_name="get_refund_timeline", expected_domain="refund", order_id=order_id,
        )

    rdata = refunds["data"] if refunds else {}
    captured = _sum_numbers(
        pdata, ("captured_amount", "captured_total", "amount_captured", "paid_amount", "total_paid")
    )
    if captured is None:
        captured = _sum_numbers(tdata, ("captured_amount", "amount_captured", "paid_amount"))
    refunded = _sum_numbers(rdata, ("refunded_amount", "refund_amount", "refunded_total", "amount_refunded"))
    refundable = _sum_numbers(rdata, ("refundable_amount", "remaining_refundable", "refund_available"))
    if refundable is None and captured is not None and refunded is not None:
        refundable = round(max(0.0, captured - refunded), 2)

    text = _lower_text({"payment": pdata, "timeline": tdata, "refund": rdata})
    verdict = "insufficient_evidence"
    if _has_any(text, ("duplicate", "duplicated", "double charge", "charged twice")):
        verdict = "duplicate_capture"
    elif _has_any(text, ("refund failed", "refund_failure", "falha no reembolso")):
        verdict = "refund_failed"
    elif _has_any(text, ("refund pending", "pending refund", "reembolso pendente")):
        verdict = "refund_pending"
    elif _has_any(text, ("refunded", "refund complete", "reembolso concluído", "reembolso concluido")):
        verdict = "refunded"
    elif captured is not None and refunded is not None and captured < refunded:
        verdict = "capture_mismatch"
    elif captured is not None:
        verdict = "reconciled"

    refs = [payments["evidence_ref"], timeline["evidence_ref"]]
    if refunds:
        refs.append(refunds["evidence_ref"])
    return {
        "verdict": verdict, "captured_total_brl": captured,
        "refunded_total_brl": refunded, "refundable_total_brl": refundable,
        "_evidence_refs": list(dict.fromkeys(refs)),
        "_payment_references": _payment_refs(pdata) or _payment_refs(tdata),
        "_data": {"payments": pdata, "timeline": tdata, "refund": rdata},
    }


async def policy_agent(case: dict, case_id: str, gateway, trace, cache) -> dict:
    evidence = await call_tool(
        gateway, trace, cache, case_id=case_id, actor="policy-agent",
        tool_name="get_policy", expected_domain="policy",
        policy_version=case.get("policy_version", "EC_POLICY_V2"),
    )
    return {"_evidence_refs": [evidence["evidence_ref"]], "_data": evidence["data"]}


async def customer_product_agent(customer_id: str | None, item_ids: list[str], case_id: str, gateway, trace, cache) -> dict:
    tasks = []
    if customer_id:
        tasks.append(call_tool(
            gateway, trace, cache, case_id=case_id, actor="customer-agent",
            tool_name="get_customer_history", expected_domain="customer",
            customer_unique_id=customer_id,
        ))
    for item_id in item_ids[:5]:
        tasks.append(call_tool(
            gateway, trace, cache, case_id=case_id, actor="product-agent",
            tool_name="get_product_context", expected_domain="product", item_id=item_id,
        ))
    if not tasks:
        return {"evidence_refs": [], "data": {}}
    results = await asyncio.gather(*tasks)
    return {"evidence_refs": [r["evidence_ref"] for r in results], "data": [r["data"] for r in results]}


# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------

PRIMARY_TOPICS = {
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed", "unsupported_claim",
}


def _claim_verdict(topic: str, shipment: dict, payment: dict, entity: dict) -> tuple[str, float, str]:
    if entity["status"] != "resolved":
        return "insufficient_evidence", 0.45, "entity"
    if topic == "late_delivery_seller":
        return ("supported", 0.96, "shipment") if shipment["verdict"] == "seller_delay" else ("unsupported", 0.92, "shipment")
    if topic == "late_delivery_logistics":
        return ("supported", 0.96, "shipment") if shipment["verdict"] == "logistics_delay" else ("unsupported", 0.92, "shipment")
    if topic == "valid_split_payment":
        return ("supported", 0.96, "payment") if payment["verdict"] == "reconciled" else ("unsupported", 0.90, "payment")
    if topic == "payment_mismatch":
        return ("supported", 0.96, "payment") if payment["verdict"] == "capture_mismatch" else ("unsupported", 0.92, "payment")
    if topic == "duplicate_charge":
        return ("supported", 0.96, "payment") if payment["verdict"] == "duplicate_capture" else ("unsupported", 0.92, "payment")
    if topic == "refund_pending":
        return ("supported", 0.97, "payment") if payment["verdict"] == "refund_pending" else ("unsupported", 0.90, "payment")
    if topic == "refund_failed":
        return ("supported", 0.97, "payment") if payment["verdict"] == "refund_failed" else ("unsupported", 0.90, "payment")
    if topic in {"canceled_order_paid", "unavailable_order_paid"}:
        text = _lower_text(entity.get("order_data", {}))
        needle = topic.replace("_", " ")
        return ("supported", 0.95, "order") if needle in text else ("unsupported", 0.90, "order")
    if topic == "unsupported_claim":
        return "unsupported", 0.96, "investigation"
    return "insufficient_evidence", 0.50, "unknown"


def _issue_from_claims(case: dict, shipment: dict, payment: dict) -> tuple[str, list[str]]:
    topics = [c.get("topic", "") for c in case.get("customer_request", {}).get("claims", [])]
    # The first claim is the case's substantive issue. requested_full_refund is
    # a requested remedy and must never displace the investigated issue.
    primary = topics[0] if topics and topics[0] in PRIMARY_TOPICS else "insufficient_evidence"
    secondary = [t for t in topics[1:] if t != "requested_full_refund" and t in PRIMARY_TOPICS]
    return primary, secondary


def _cause_and_party(issue: str, shipment: dict) -> tuple[list[dict], list[dict]]:
    mapping = {
        "late_delivery_seller": ("SELLER_LATE_SHIPMENT", "seller"),
        "late_delivery_logistics": ("LOGISTICS_DELAY", "logistics_provider"),
        "duplicate_charge": ("DUPLICATE_PAYMENT_CAPTURE", "payment_provider"),
        "payment_mismatch": ("PAYMENT_RECONCILIATION_MISMATCH", "payment_provider"),
        "refund_pending": ("REFUND_PENDING", "platform"),
        "refund_failed": ("REFUND_FAILED", "platform"),
        "canceled_order_paid": ("CANCELED_ORDER_PAYMENT", "platform"),
        "unavailable_order_paid": ("UNAVAILABLE_ORDER_PAYMENT", "platform"),
        "valid_split_payment": ("VALID_SPLIT_PAYMENT", "unknown"),
        "unsupported_claim": ("UNSUPPORTED_CLAIM", "unknown"),
    }
    if issue not in mapping:
        return [], []
    cause, party_type = mapping[issue]
    sellers = shipment.get("late_seller_ids", [])
    if party_type == "seller" and sellers:
        parties = [{"party_type": "seller", "party_id": sellers[0]}]
    elif party_type == "unknown":
        parties = [{"party_type": "unknown", "party_id": None}]
    else:
        parties = [{"party_type": party_type, "party_id": None}]
    return [{"cause_code": cause, "rank": 1}], parties


# ---------------------------------------------------------------------------
# Deterministic adjudication
# ---------------------------------------------------------------------------

# The workflow intentionally does not call an external LLM. MCP evidence and
# the case's documented claim topics are the only sources used for adjudication.
# This keeps tool traces deterministic and avoids an unnecessary network call
# during scoring.

# ---------------------------------------------------------------------------
# Output / verification
# ---------------------------------------------------------------------------

def verify(entity: dict, evidence_refs: list[str], *, claim_assessments: list[dict], case: dict) -> list[str]:
    problems: list[str] = []
    if entity["status"] != "resolved":
        problems.append(f"entity_{entity['status']}")
    if not evidence_refs:
        problems.append("no_evidence_collected")
    if any(not c.get("evidence_refs") for c in claim_assessments):
        problems.append("claim_without_evidence")
    if case.get("investigation_scope", {}).get("require_independent_verification") and len(evidence_refs) < 2:
        problems.append("insufficient_independent_evidence")
    return problems


def build_output(*, case_id: str, entity: dict, shipment: dict, payment: dict,
                 claim_assessments: list[dict], data_conflicts: list[dict],
                 evidence_refs: list[str], issue: str, secondary: list[str],
                 causes: list[dict], parties: list[dict], item_ids: list[str],
                 refund_brl: float, refund_reason: str | None, confidence: float) -> dict:
    status = "action_required" if issue not in {"unsupported_claim", "valid_split_payment", "insufficient_evidence"} else "no_action"
    if issue == "insufficient_evidence":
        status = "needs_investigation"
    refund_lines = []
    if refund_brl > 0:
        refund_lines.append({
            "reason_code": refund_reason or issue,
            "amount_brl": round(refund_brl, 2),
            "entity_id": entity["resolved_order_ids"][0] if entity["resolved_order_ids"] else None,
        })
    actions: list[str] = []
    if refund_brl > 0:
        actions.append("issue_full_refund")
    if issue == "late_delivery_seller":
        actions.append("notify_seller")
    elif issue == "late_delivery_logistics":
        actions.append("escalate_logistics")
    elif issue in {"payment_mismatch", "duplicate_charge"}:
        actions.append("reconcile_payment")
    elif issue == "refund_pending":
        actions.append("monitor_refund")
    elif issue == "refund_failed":
        actions.append("retry_refund_review")

    return {
        "schema_version": "day09-l3b-output-v2", "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": list(dict.fromkeys(secondary))[:10],
            "case_status": status,
            "confidence": round(max(0.0, min(1.0, confidence)), 2),
        },
        "affected_entities": {
            "order_ids": entity.get("resolved_order_ids", []),
            "item_ids": item_ids,
            "seller_ids": shipment.get("late_seller_ids", []),
            "payment_references": payment.get("_payment_references", []),
            "shipment_ids": shipment.get("_shipment_ids", []),
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity["status"], "resolved_order_ids": entity["resolved_order_ids"],
            "rejected_candidates": entity["rejected_candidates"], "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity.get("customer_unique_id"),
            "related_order_ids": entity.get("resolved_order_ids", []),
        },
        "shipment_analysis": {
            "verdict": shipment["verdict"], "late_seller_ids": shipment["late_seller_ids"],
            "timeline_complete": shipment["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment["verdict"], "captured_total_brl": payment["captured_total_brl"],
            "refunded_total_brl": payment["refunded_total_brl"], "refundable_total_brl": payment["refundable_total_brl"],
        },
        "root_cause_analysis": {"ranked_causes": causes, "responsible_parties": parties},
        "evidence_refs": evidence_refs,
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": round(refund_brl, 2),
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }


async def solve_case(case: dict, gateway, trace) -> dict:
    case_id = case["case_id"]
    cache = ToolCache()
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="entity-agent")
    entity = await resolve_entity(case, gateway, trace, cache)
    if entity["status"] != "resolved":
        trace.emit(
            case_id=case_id, event_type="verification_completed", actor="verifier",
            decision_code="entity_" + entity["status"],
        )
        return build_output(
            case_id=case_id, entity=entity,
            shipment={"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False, "_shipment_ids": []},
            payment={"verdict": "insufficient_evidence", "captured_total_brl": None, "refunded_total_brl": None, "refundable_total_brl": None, "_payment_references": []},
            claim_assessments=[], data_conflicts=[], evidence_refs=entity["evidence_refs"],
            issue="insufficient_evidence", secondary=[], causes=[], parties=[], item_ids=[],
            refund_brl=0, refund_reason=None, confidence=entity["confidence"],
        )

    order_id = entity["resolved_order_ids"][0]
    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="specialists")
    order, shipment, payment, policy = await asyncio.gather(
        order_agent(order_id, case_id, gateway, trace, cache),
        shipment_agent(order_id, case_id, gateway, trace, cache),
        payment_refund_agent(order_id, case, case_id, gateway, trace, cache),
        policy_agent(case, case_id, gateway, trace, cache),
    )
    customer_product = await customer_product_agent(
        entity.get("customer_unique_id"), order["item_ids"], case_id, gateway, trace, cache,
    )

    claims = case.get("customer_request", {}).get("claims", [])
    claim_assessments: list[dict] = []
    claim_objs: list[Claim] = []
    base_refs = list(dict.fromkeys(
        entity["evidence_refs"] + order["evidence_refs"] + shipment["_evidence_refs"]
        + payment["_evidence_refs"] + policy["_evidence_refs"] + customer_product["evidence_refs"]
    ))
    for claim in claims[:5]:
        topic = claim.get("topic", "")
        verdict, confidence, source = _claim_verdict(topic, shipment, payment, entity)
        if topic == "requested_full_refund":
            # Remedy request: supported only when the substantive issue and
            # policy/payment evidence permit a refund. Do not call refund lookup
            # merely because this request exists.
            refundable = payment.get("refundable_total_brl")
            allowed = refundable is not None and refundable > 0 and claims and claims[0].get("topic") not in {"unsupported_claim", "valid_split_payment"}
            verdict = "supported" if allowed else "unsupported"
            confidence = 0.92 if allowed else 0.90
            source = "payment-policy"
        if topic in {"late_delivery_seller", "late_delivery_logistics"}:
            refs = shipment["_evidence_refs"]
        elif topic in {"valid_split_payment", "payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed", "requested_full_refund"}:
            refs = list(dict.fromkeys(payment["_evidence_refs"] + policy["_evidence_refs"]))
        elif topic in {"canceled_order_paid", "unavailable_order_paid"}:
            refs = entity["evidence_refs"]
        else:
            refs = base_refs
        claim_assessments.append({
            "claim_id": claim["claim_id"], "verdict": verdict,
            "confidence": round(confidence, 2), "evidence_refs": refs[:30],
        })
        claim_objs.append(Claim(topic, verdict, confidence, refs, source))

    _, conflicts = _resolve_claim_conflicts(claim_objs, case_id, trace)
    issue, secondary = _issue_from_claims(case, shipment, payment)

    refund = 0.0
    refund_reason = None
    refundable = payment.get("refundable_total_brl")
    first_claim = claims[0].get("topic") if claims else ""
    if refundable is not None and refundable > 0 and first_claim not in {"unsupported_claim", "valid_split_payment"}:
        refund = round(refundable, 2)
        refund_reason = issue

    causes, parties = _cause_and_party(issue, shipment)
    confidence_values = [c["confidence"] for c in claim_assessments] or [entity["confidence"]]
    confidence = min(entity["confidence"], sum(confidence_values) / len(confidence_values))
    all_refs = list(dict.fromkeys(base_refs))[:30]
    problems = verify(entity, all_refs, claim_assessments=claim_assessments, case=case)
    decision = "ok" if not problems else ",".join(problems)
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier", decision_code=decision)

    return build_output(
        case_id=case_id, entity=entity, shipment=shipment, payment=payment,
        claim_assessments=claim_assessments, data_conflicts=conflicts,
        evidence_refs=all_refs, issue=issue, secondary=secondary,
        causes=causes, parties=parties, item_ids=order["item_ids"],
        refund_brl=refund, refund_reason=refund_reason, confidence=confidence,
    )


def _resolve_claim_conflicts(claims: list[Claim], case_id: str, trace) -> tuple[list[Claim], list[dict]]:
    by_field: dict[str, list[Claim]] = {}
    for claim in claims:
        by_field.setdefault(claim.field, []).append(claim)
    resolved: list[Claim] = []
    conflicts: list[dict] = []
    for field_name, group in by_field.items():
        if len({repr(c.value) for c in group}) == 1:
            resolved.append(group[0])
            continue
        best = max(group, key=lambda c: c.confidence)
        sources = list(dict.fromkeys(c.source_agent for c in group))
        conflict = {
            "field": field_name, "sources": sources[:5],
            "selected_source": best.source_agent,
            "resolution_code": "highest_confidence_source",
        }
        conflicts.append(conflict)
        trace.emit(
            case_id=case_id, event_type="policy_decided", actor="conflict-resolver",
            target=field_name, decision_code="highest_confidence_source",
        )
        resolved.append(best)
    return resolved, conflicts
