# L3B Multi-Agent MCP + A2A Architecture

## 1. Purpose

This submission implements an evidence-first L3B investigation workflow. The
coordinator resolves the order, delegates independent investigations to
specialists, reconciles their findings, verifies the assembled result, and
writes a schema-valid case output.

MCP is the authoritative data/evidence boundary. A2A-style collaboration is
represented by explicit coordinator handoffs and typed specialist results. No
specialist invents facts or shares mutable MCP state with another case.

```text
case_received
     |
     v
Coordinator -> Entity Agent -> authoritative order resolution
     |
     +---- handoff ----+---------+----------+----------+
     |                  |         |          |          |
     v                  v         v          v          v
 Order/Product       Shipment  Payment    Policy    Customer/Product
     |                  |         |          |          |
 get_order_items   get_shipment  payments  get_policy  customer history
 get_product       summary       timeline             product context
                                  |
                           conditional refund lookup
     +--------------------+-------+-------------------+
                          |
                    Claim assessment
                          |
                    Conflict resolver
                          |
                    Deterministic adjudication
                          |
                       Verifier
                          |
                    case_finalized
```

## 2. MCP tool ownership

The discovered MCP tool set is:

```text
get_customer_history
get_order
get_order_items
get_order_payments
get_payment_timeline
get_policy
get_product_context
get_refund_timeline
get_sellers
get_shipment_summary
```

| Specialist | Tools | Output |
|---|---|---|
| Entity | `get_order` | resolved order/customer + evidence ref |
| Order/Product | `get_order_items`, `get_product_context` | item/seller context |
| Shipment | `get_shipment_summary` | delivery verdict, seller responsibility, shipment refs |
| Payment | `get_order_payments`, `get_payment_timeline` | capture/payment reconciliation |
| Refund | `get_refund_timeline` only when refund-state evidence is relevant | refund lifecycle |
| Policy | `get_policy` | policy evidence |
| Customer | `get_customer_history` | related customer context |

The workflow deliberately does not call `get_refund_timeline` for every order.
`requested_full_refund` is a requested remedy, not proof that a refund record
exists. Refund lookup is triggered only for refund-state claims (`refund_pending`
or `refund_failed`) or when payment evidence itself contains an explicit refund
signal.

If `get_refund_timeline` returns a normal "not found"/tool error, the workflow
treats that as **absence of refund evidence**, does not retry it, and continues.
This is an expected business state rather than a workflow failure.

## 3. Entity resolution

The claimed order ID is checked against authoritative `get_order` evidence. If
that lookup is unavailable, candidate orders are evaluated individually; an
order is never fabricated or silently replaced by a different ID.

Resolution states are:

- `resolved`: authoritative evidence identifies one order;
- `ambiguous`: multiple candidates remain too close to select safely;
- `not_found`: no candidate can be established.

The selected order and rejected candidates are preserved in the output.

## 4. Specialist handoff and evidence lifecycle

Every consumed MCP result is validated against the public MCP evidence schema
and contributes its server-issued `evidence_ref` to the case trace. A fresh
per-case cache prevents duplicate calls without allowing evidence to leak
between cases.

Independent calls are concurrent where safe:

- order items, shipment, payment/timeline, and policy after entity resolution;
- customer history and product context after item resolution;
- refund lookup is conditional, not unconditional.

The workflow never fabricates an evidence reference. Final evidence is the
union of evidence actually consumed for that case.

## 5. Claim semantics

The first claim in each L3B case is the **substantive issue**. The recurring
`requested_full_refund` claim is treated as a requested remedy and never
replaces the investigated issue.

Claim assessment is evidence-based:

- shipment claims use shipment evidence;
- payment claims use payment/timeline evidence;
- cancellation/unavailability claims use order evidence;
- refund requests use payment + policy evidence;
- unsupported claims remain unsupported rather than being converted into an
  action merely because the customer asked for it.

The primary issue therefore comes from the case's substantive claim and is not
blindly replaced by the requested remedy.

## 6. Refund decision gate

The refund path is intentionally asymmetric:

```text
payment + payment timeline
          |
          +-- refund-state signal? -- NO --> do not call refund timeline
          |
         YES
          |
          v
get_refund_timeline
          |
          +-- refund exists --> reconcile refund totals
          |
          +-- refund not found --> no refund evidence; no retry
```

A recommended refund cannot exceed the authoritative refundable amount. A
`valid_split_payment` or `unsupported_claim` does not automatically become a
refund action merely because the customer requested a full refund.

## 7. Deterministic adjudication

The workflow does not call an external LLM during evaluation. Primary issue
adjudication is derived from the case claim topics plus authoritative MCP
evidence. This keeps the tool trace deterministic, avoids an extra network
dependency, and prevents an external model from changing the case decision.

When evidence is insufficient or conflicting, the workflow preserves
`insufficient_evidence` rather than inventing a new conclusion.

## 8. Verification and scoring safeguards

Before final output, the verifier checks:

- resolved entity scope;
- evidence references are real and case-scoped;
- every claim has evidence;
- independent evidence is present when requested by the case;
- confidence remains within `[0,1]`;
- refund amount does not exceed the evidence-backed refundable amount;
- primary issue is within the L3B allowed-value set;
- resolution actions are consistent with the selected issue.

The CLI already emits `case_received` and the final `case_finalized` event, so
`workflow.py` emits the intermediate lifecycle events (`task_assigned`,
`handoff`, `verification_completed`) without duplicating `case_finalized`.

## 9. MCP SDK compatibility

The starter gateway uses the legacy `CallToolResult.isError` property while
current MCP SDK releases expose `is_error`. Because only `workflow.py` and this
document are edited, `workflow.py` installs a compatibility implementation of
`EvidenceGateway.call` that accepts either spelling and either structured
content spelling.

## 10. Reproducibility

Run from the repository root:

```powershell
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Secrets remain in `.env`/environment variables and are never written into the
output artifacts or architecture document.
