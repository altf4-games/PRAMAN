/**
 * Typed client for the PRAMAN REST API (api/praman/schemas.py). Every shape
 * here mirrors a Pydantic model on the backend by name; keep them in sync
 * by hand when a route's response shape changes — there's no shared
 * codegen between the two languages in this build.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = await res.text();
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// --- shapes ---

export interface ProductOut {
  id: string;
  sku: string;
  name: string;
  category: string;
  category_class: string;
  unit_price_paise: number;
  stock: number | null;
  return_window_days: number | null;
  fulfilment_hours: number | null;
  is_personalised: boolean;
}

export interface ReviewProductOut {
  id: string;
  sku: string;
  name: string;
  category: string;
  unit_price_paise: number;
  field_confidence: Record<string, number>;
  source: string;
  source_media_url: string | null;
}

export interface PolicyOut {
  merchant_id: string;
  name: string;
  did: string;
  max_txn_paise: number | null;
  cooling_off_hold: boolean | null;
}

export interface MerchantOut {
  id: string;
  name: string;
  did: string;
  whatsapp_number: string;
  onboarding_state: string;
  agent_policy: Record<string, unknown>;
}

export interface AgentRegisterOut {
  agent_did: string;
  public_key: string;
  private_key: string | null;
}

export interface EnvelopeOut {
  envelope_id: string;
  merchant_id: string;
  agent_did: string;
  ceiling_paise: number;
  spent_paise: number;
  max_single_txn_paise: number;
  allowed_categories: string[];
  min_reversibility: number;
  valid_from: string;
  valid_until: string;
  signature: string;
}

export interface QuoteOut {
  quote_id: string;
  product_id: string;
  sku: string;
  category: string;
  agent_did: string;
  merchant_did: string;
  unit_price_paise: number;
  qty: number;
  total_paise: number;
  stock_held: boolean;
  issued_at: string;
  expires_at: string;
  nonce: string;
  signature: string;
}

export interface CartConfirmOut {
  cart_id: string;
  subtotal_paise: number;
  total_paise: number;
  reversibility_score: number;
  reversibility_breakdown: Record<string, number>;
  band: "green" | "amber" | "red";
  envelope_check_decision: string;
  envelope_check_reason_code: string;
}

export interface SubstitutionCandidateOut {
  product_id: string;
  sku: string;
  name: string;
  unit_price_paise: number;
}

export interface CheckoutExecuteOut {
  decision: "ALLOW" | "HOLD" | "ESCALATE" | "SUBSTITUTE" | "BLOCK";
  reason_code: string;
  detail: string;
  remedy: string;
  order_id: string | null;
  order_status: string | null;
  substitution_offer: SubstitutionCandidateOut[];
  substitution_rationale: string | null;
}

export interface OrderOut {
  id: string;
  cart_id: string;
  status: string;
  razorpay_order_id: string | null;
  cooling_off_until: string | null;
  dispatched_at: string | null;
  cancelled_at: string | null;
  refunded_at: string | null;
  amount_paise: number | null;
  razorpay_key_id: string | null;
}

export interface AgentRunOut {
  run_id: string;
  agent_did: string | null;
  cart_id: string | null;
  order_id: string | null;
  decision: string | null;
  summary: string;
}

export interface CheckoutConfirmOut {
  order_id: string;
  status: string;
  dispatched_at: string | null;
  cooling_off_until: string | null;
}

export interface ApprovalOut {
  order_id: string;
  cart_id: string;
  merchant_id: string;
  item_summary: string;
  total_paise: number;
  reason_code: string;
  reversibility_score: number;
  reversibility_breakdown: Record<string, number>;
  band: "green" | "amber" | "red";
  created_at: string;
  deadline: string;
}

export interface DisputePackOut {
  cart_id: string;
  envelope: Record<string, unknown>;
  cart_mandate: Record<string, unknown>;
  order: Record<string, unknown> | null;
  gate_trail: Array<Record<string, unknown>>;
  quote_provenance: Array<Record<string, unknown>>;
  reversibility_breakdown: Record<string, number>;
  band: "green" | "amber" | "red";
  ledger: {
    session_id: string;
    events: Array<{
      event_id: string;
      ts: string;
      event_type: string;
      agent_did: string | null;
      payload: Record<string, unknown>;
      payload_hash: string;
      prev_hash: string;
      chain_hash: string;
    }>;
    chain_verified: boolean;
  };
  merchant_did: string | null;
  pack_hash: string;
  merchant_signature: string | null;
}

export interface MetricsOut {
  sessions_gated: number;
  orders_by_status: Record<string, number>;
  orders_by_band: Record<string, number>;
  disputes_resolvable: number;
  escalations_ever: number;
}

// --- calls ---

export const api = {
  catalogSearch: (merchantId: string, params?: { category?: string; q?: string }) =>
    request<ProductOut[]>(
      `/api/catalog/search?${new URLSearchParams({ merchant_id: merchantId, ...params })}`,
    ),
  catalogGet: (productId: string) => request<ProductOut>(`/api/catalog/${productId}`),
  catalogReviewQueue: (merchantId: string) =>
    request<ReviewProductOut[]>(
      `/api/catalog/review-queue?${new URLSearchParams({ merchant_id: merchantId })}`,
    ),
  policyGet: (merchantId: string) => request<PolicyOut>(`/api/policy/${merchantId}`),

  listMerchants: () => request<MerchantOut[]>("/api/merchants"),
  getMerchant: (id: string) => request<MerchantOut>(`/api/merchants/${id}`),

  registerAgent: (body: { operator: string; max_txn_paise: number; daily_cap_paise: number }) =>
    request<AgentRegisterOut>("/api/agents/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  submitEnvelope: (body: {
    merchant_id: string;
    agent_did: string;
    user_ref: string;
    user_whatsapp: string;
    ceiling_paise: number;
    max_single_txn_paise: number;
    allowed_categories: string[];
    min_reversibility: number;
    valid_hours: number;
  }) => request<EnvelopeOut>("/api/envelopes", { method: "POST", body: JSON.stringify(body) }),

  requestQuote: (raw: string, signed: { timestamp: string; nonce: string; signature: string }) =>
    signedPost<QuoteOut>("/api/quotes", raw, signed),

  confirmCart: (raw: string, signed: { timestamp: string; nonce: string; signature: string }) =>
    signedPost<CartConfirmOut>("/api/cart/confirm", raw, signed),

  executeCheckout: (raw: string, signed: { timestamp: string; nonce: string; signature: string }) =>
    signedPost<CheckoutExecuteOut>("/api/checkout/execute", raw, signed),

  orderStatus: (orderId: string) => request<OrderOut>(`/api/orders/${orderId}`),
  orderUndo: (orderId: string, userRef: string) =>
    request<{ cancelled: boolean }>(`/api/orders/${orderId}/undo`, {
      method: "POST",
      body: JSON.stringify({ user_ref: userRef }),
    }),
  checkoutConfirm: (
    orderId: string,
    body: { razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string },
  ) =>
    request<CheckoutConfirmOut>(`/api/checkout/${orderId}/confirm`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  approvalsInbox: (merchantId: string) =>
    request<ApprovalOut[]>(`/api/approvals?${new URLSearchParams({ merchant_id: merchantId })}`),
  approvalsDecide: (orderId: string, decision: "approve" | "decline") =>
    request<{ decision: string; order_status: string }>(`/api/approvals/${orderId}/decide`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),

  agentRun: (body: {
    run_id: string;
    goal: string;
    merchant_id: string;
    merchant_name: string;
    user_ref: string;
    user_whatsapp: string;
    ceiling_paise: number;
    max_single_txn_paise: number;
    allowed_categories: string[];
    min_reversibility: number;
  }) => request<AgentRunOut>("/api/agent/run", { method: "POST", body: JSON.stringify(body) }),

  disputePack: (cartId: string) => request<DisputePackOut>(`/api/dispute-pack/${cartId}`),
  metrics: () => request<MetricsOut>("/api/metrics"),
};

/** `raw` must be the exact JSON string `signRequest` (lib/sign.ts) hashed —
 * re-serializing the body here instead would risk a byte-for-byte mismatch
 * with what was actually signed. */
async function signedPost<T>(
  path: string,
  raw: string,
  signed: { timestamp: string; nonce: string; signature: string },
): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: raw,
    headers: {
      "X-Praman-Timestamp": signed.timestamp,
      "X-Praman-Nonce": signed.nonce,
      "X-Praman-Signature": signed.signature,
    },
  });
}
