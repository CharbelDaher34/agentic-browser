# Approval gate redesign — GroundGate

> Status: **partially implemented.** The M0 core — ground-truth classification for both
> DOM and vision, an expanded lexicon, escape-hatch handling, and the MCP `AUTO_APPROVE`
> fail-open fix — is **done and live-verified** (see §"Already done"). The remaining M0/M1
> items (TOCTOU nonce, payment-host fail-closed, `press_key`/`drag`/`navigate` routing,
> sub-agent re-surfacing, spend ceiling, multilingual, model second-opinion) are still
> planned.

## The problem

The approval gate must reliably pause destructive web actions (pay / buy / delete /
send / checkout) for human approval, and it must work for **vision mode** (coordinate
clicks), which is where it is weakest today.

**The core hole.** In `agenticbrowser/agent.py`, the vision tool classifies risk on a
model-supplied label that defaults to empty:

```python
async def click_at(ctx, x, y, label=""):
    risk = _classify("click", label, None)   # label="" -> Risk.SAFE -> clicks unapproved
```

So the only thing standing between the agent and an unapproved "Place order" click is
the model choosing to truthfully describe what it's clicking. A forgetful or
adversarially-steered model that calls `click_at(x, y)` with no label gets `Risk.SAFE`
and executes with no approval. DOM clicks (`act(ref, ...)`) are safer only because their
label comes from the page's collected elements, not the model.

**Principle:** the safety decision must be computed from the page under the cursor, not
from a string the model typed.

## Approaches considered

Five independent designs were generated and adversarially critiqued (attacker lens +
UX lens). They converge on the same spine.

| Approach | Idea | Outcome |
|---|---|---|
| **Page-grounded hit-test** | `elementFromPoint(x,y)` recovers the real target; classify on that | Chosen as the core |
| **Model-assisted classifier** | Cheap cached VLM second-opinion on the screenshot region for ambiguous targets | Optional escalation layer (default off) |
| **Defense-in-depth policy** | Pluggable `ApprovalPolicy` composing taxonomy + multilingual lexicon + URL rules + spend ceiling | Adopted as the layering structure |
| **Capability allowlist** | Permit only declared domains/actions; gate everything else | Too much UX friction as primary; its escalators folded in |
| **Minimal pragmatic** | Smallest diff: derive label from hit-test, fail-closed on unclassifiable interactive targets | Became the M0 shippable slice |

## Recommended design: GroundGate

A page-grounded, fail-closed gate. The safety decision comes from
`document.elementFromPoint` + the live URL, never from the model's label. Every gated
action (`act`, `click_at`, `type_at`, `press_key`, `drag`, `navigate`) flows through one
async `_authorize` → `classify_facts` → `_gate` path that combines layers by **max-risk**
(model input and page-rendered amounts can only *escalate*, never de-escalate), and
unresolvable-yet-interactive targets fail closed.

### Layered architecture (firing order, `SAFE < SENSITIVE < DESTRUCTIVE`)

- **L0 — structural fast-path (no I/O).** `scroll`/`wait`/`go_back`/`go_forward` and
  benign `navigate` short-circuit. Keeps the common case free.
- **L1 — page-grounded hit-test** (`session.hit_test`, one `page.evaluate`, ~1-5ms, $0)
  for coordinate tools; DOM `act` reuses the already-collected `Element` (no extra
  evaluate). Both produce one `TargetFacts`.
- **L2 — ground-truth verb/structure classify** over the *resolved* element's
  text/aria/value/href/input_type. `input[type=password|email|tel]` or `kind in
  (type, select)` → SENSITIVE; `type=submit` / in-`<form>`+submitting → escalate.
- **L3 — URL / egress / spend escalators (escalate-only).** Top-URL or link/form-action
  host on `payment_domains`; a cross-origin payment-iframe `src` host at the click point;
  `navigate` target matching `destructive_url_re`; a currency amount above
  `spend_ceiling_usd`. Absence never lowers risk.
- **L4 — fail-closed floor.** Hit-test null/throws, or an **interactive** element with
  empty ground-truth text on a payment/auth host → DESTRUCTIVE. (Empty-text interactive
  on a *non-payment* host stays SENSITIVE, to avoid pausing every hamburger/X/chevron.)
- **L5 — the gate** (`_gate`, unchanged seam): depth 0 raises `ApprovalRequired`; depth
  ≥ 1 routes to the orchestrator (new mechanism, below).
- **L6 (M2, default off) — model second opinion** via `build_vision_model(cfg)`,
  escalate-only, cached on DOM-identity, only in the ambiguous-interactive band.

### Unified vision+DOM flow — empty-label `click_at(x, y, label="")` end to end

1. `click_at` calls `verdict = await _authorize(ctx, "click", x=x, y=y, label=label)`.
   **`label` is folded in only as an escalator token; it is never read for the floor
   decision.**
2. `_authorize` → `resolve_target` → `session.hit_test(x, y, tab_id)` runs
   `document.elementFromPoint(x,y)`, climbs via `composedPath()` (shadow-DOM-aware) to
   the nearest interactive ancestor, and returns `TargetFacts{tag, role, text, aria,
   value, href, input_type, in_form, interactive, is_payment_iframe, iframe_src_host,
   near_amount, dpr, nonce}`. It stamps the resolved node `data-gate=<nonce>`.
3. `classify_facts` runs L2-L4. A real "Place order — $128.40" submit →
   `type=submit`+in_form structural escalation and/or `near_amount > spend_ceiling` →
   DESTRUCTIVE. (The verb regex alone does **not** match "place order"; the structural +
   amount escalators carry it.)
4. L4 floor: hit-test threw / interactive-empty-on-payment-host / cross-origin payment
   iframe → DESTRUCTIVE.
5. `_gate(ctx, DESTRUCTIVE)` at depth 0 → `raise ApprovalRequired` → PydanticAI emits
   `DeferredToolRequests` → `runner._collect` emits an `approval_request` enriched with
   action + resolved role/name + host — **NOT** the page-supplied amount as ground truth.
   Blocks on the no-timeout future, resumes with `ToolApproved()`/`ToolDenied()`.
6. **TOCTOU pin + post-approval re-classify.** On approval, the action re-executes;
   `_perform` re-runs `elementFromPoint(x,y)` under the tab action-lock and asserts the
   `data-gate` nonce still matches before `page.mouse.click`. For DESTRUCTIVE verdicts it
   also re-runs `classify_facts` at act-site and aborts if the verdict diverges in
   host/amount. Mismatch → raise → no click → fail-closed.
7. **Sub-agent path (depth ≥ 1).** `_gate` returns a sentinel `_SubAgentBlocked(facts)`;
   `_run_subtask` returns `{"tab": …, "blocked": facts}` (not an exception, so it isn't
   swallowed by `gather(return_exceptions=True)`). `spawn_subagents` detects blocked
   results and **re-issues the destructive action on the orchestrator's own tab under
   `ApprovalRequired`** (or surfaces a first-class `approval_request`). Sub-agents still
   cannot self-approve.
8. **Approval-prompt spoofing fix.** Any page-supplied amount is shown only as
   "page-claimed $X (unverified)" or omitted; approval is based on action + host +
   target-identity, never a number the adversary renders.

### File-by-file changes

- **NEW `agenticbrowser/approval.py`** — `TargetFacts` (frozen) + a fail-closed factory;
  `Verdict(risk, reason)`; `combine_max`; the verb/url/amount/safe-hatch regexes;
  `classify_facts(facts, *, kind, submit, url, policy) -> Verdict`; `ApprovalPolicy`
  (the config knobs).
- **`agenticbrowser/session.py`** — NEW `hit_test(x,y,tab_id)` (`_HITTEST_JS`: normalize
  `devicePixelRatio`, `composedPath()` climb, detect cross-origin iframe at point and
  return its `src` host, stamp `data-gate` nonce, reuse `observe()`'s context-destroyed
  retry, null/throw → fail-closed dict). EXTEND `_COLLECT_JS` to also emit `href`,
  `type`, `bbox` (the bounding box is *already computed* there — zero added cost). EDIT
  `_perform` for the nonce re-check + DESTRUCTIVE post-approval re-classify before
  `page.mouse.click`.
- **`agenticbrowser/agent.py`** — rewrite `_classify` → `async def _authorize(...) ->
  Verdict`; NEW `resolve_target`. Route `act`, `click_at`, `type_at` (+enclosing-form on
  `press_enter`), `press_key` (+activeElement/enclosing-form, +host-based fail-closed when
  no control is resolvable on a payment/auth host), `drag` (both endpoints, drop hardcoded
  SAFE), `navigate` (run url checks before `goto`) through `_authorize`. `_gate`: keep
  depth-0 raise; depth ≥ 1 → `_SubAgentBlocked` sentinel. Catch-all in `_authorize` →
  `Verdict(DESTRUCTIVE, "classifier error")`. Make `label` advisory in prompts, not the
  safety mechanism.
- **`agenticbrowser/mcp/server.py`** — ✅ **done** (see §"Already done").
- **`agenticbrowser/models.py`** — `Element`: add optional `href`, `input_type`, `bbox`
  (keyword/optional → back-compat); update `to_json`. `Action`: add optional
  `gate_nonce`. No DB migration (`Element` is transient).
- **`agenticbrowser/config.py`** — add policy knobs to `CoreConfig`; thread through
  **both** producers (SDK constructor + `Settings.to_core_config()`) with a propagation
  test.
- **`agenticbrowser/runner.py`** — seam untouched except `_collect` adds an optional
  `reason` key to `approval_request` (host/role/name only — no unverified amount).
- **`agenticbrowser/events.py`** — add optional `"reason"` to
  `EVENT_DATA_KEYS["approval_request"]`; bump `EVENT_SCHEMA_VERSION` `"1.0" → "1.1"`
  (additive ⇒ MINOR); update `tests/test_event_contract.py`.

### Config + defaults

```
gate_enabled: bool = True
gate_shadow_mode: bool = True                  # M0 ships in shadow first: compute+log, don't enforce
fail_closed_on_unresolved: bool = True         # null/throw/payment-iframe/empty-interactive-on-payment-host -> DESTRUCTIVE
gate_sensitive_submits: bool = True            # form-submit on payment/auth host -> pause
payment_domains: tuple[str,...] = ("paypal.", "stripe.", "checkout.", "adyen.", "braintree", "/checkout", "/pay", "pay.")
destructive_url_re: str | None = None          # extends delete|transfer|wire|confirm|unsubscribe|deactivate|finalize
spend_ceiling_usd: float | None = None         # opt-in; escalate-only, never gates by absence
extra_destructive_verbs: tuple[str,...] = ()
safe_hatch_verbs: tuple[str,...] = ("cancel", "close", "dismiss", "back", "no", "keep", "not now")  # NEVER destructive
gate_enable_model_classifier: bool = False     # L6 off
classifier_min_confidence: float = 0.6
classifier_timeout_ms: int = 800
hittest_retry: int = 1
```

Expanded verb list (best-effort, never the floor):
`order|purchase|place order|wire|withdraw|authorize|finalize|terminate|deactivate` — but
`safe_hatch_verbs` are stripped before matching, and high-frequency benign tokens
(`remove`, `cancel`, `order`) are **not** in the destructive list ("Cancel" / "Remove
from cart" / "Order history" must not gate). The spend ceiling fires only on
submit/checkout-context buttons, not add-to-cart or listing prices. Fast path stays
cheap: only the four coordinate tools pay one hit-test; L6 off ⇒ zero steady-state token
cost. `fail_closed_on_unresolved` is **not** a global off-switch — even relaxed, an
unresolved click on a `payment_domains` host stays DESTRUCTIVE.

### Fail-safe rules

Uncertainty → pause. (1) hit-test null/throws → one retry → still unresolved →
DESTRUCTIVE. (2) cross-origin payment iframe at point → DESTRUCTIVE (one-time per
checkout). (3) nonce mismatch or DESTRUCTIVE post-approval divergence → `_perform` raises,
no click. (4) L6 raise/timeout/low-confidence/missing-key → DESTRUCTIVE. (5) `_authorize`
raises → DESTRUCTIVE. (6) model `label` / page amount escalate-only. (7) sub-agents can't
self-approve; blocked → orchestrator. (8) **MCP can never auto-approve DESTRUCTIVE**
(done). (9) SDK `approve=None` auto-denies. (10) no approver wired → `_collect` future
denied.

### Phased rollout

- **M0 (minimal correct fix).** `hit_test` + `_COLLECT_JS` href/type/bbox;
  `click_at`/`type_at` through `resolve_target` → `classify_facts` → `_gate`; nonce pin +
  DESTRUCTIVE post-approval re-classify; fail-closed floor (payment-host-scoped). Ship
  behind `gate_shadow_mode=True` (compute + log, keep current behavior) to measure
  false-positive rate before enforcing.
- **M1.** Route `press_key`/`drag`/`type_at`-submit/`navigate`-target; cross-origin
  payment-iframe `src`-host escalator with one-time-per-checkout approval; sub-agent →
  orchestrator surfacing; safe-hatch exclusion + expanded verbs; approval-prompt
  enrichment (no unverified amount) + events 1.1. Thread knobs through both producers.
- **M2.** Opt-in `spend_ceiling_usd` (submit-context only); L6 model second-opinion (cache
  only escalations, keyed on DOM identity); per-layer telemetry into `StepRecord`.

### Test plan (`tests/test_approval_gate.py`, dependency-free like `tests/test_no_env_keys.py`)

1. **Core regression:** `click_at(x,y,label="")` over a `type=submit` "Place order"
   in-form node → DESTRUCTIVE → depth-0 `ApprovalRequired`; assert empty label never
   consulted.
2. **No false-positive:** non-interactive `<div>`/background → SAFE; benign `<a>` "Read
   more" on non-payment host → SAFE; icon-only X/hamburger → not DESTRUCTIVE.
3. **Fail-closed:** hit-test raises/null / empty-interactive-on-payment-host /
   cross-origin-payment-iframe → DESTRUCTIVE.
4. **Safe-hatch:** "Cancel" / "Close" / "Remove from cart" / "Order history" → NOT
   DESTRUCTIVE.
5. **Spend cap:** `near_amount=142` + ceiling=100 on a checkout-submit → DESTRUCTIVE;
   same on add-to-cart/listing → not; ceiling=None → not.
6. **DOM/vision parity:** `act(ref)` and `click_at` over identical facts → identical
   `Verdict`.
7. **press_key / type_at-submit / drag:** Enter on activeElement in a payment form →
   DESTRUCTIVE; Enter with no resolvable control on a payment host → DESTRUCTIVE; drag
   both-endpoint slide-to-confirm → gated.
8. **navigate target:** `…/transfer?amount=500&confirm=1` → DESTRUCTIVE pre-`goto`;
   `data:` / userinfo-`@` → escalate.
9. **TOCTOU:** `_perform` re-resolution returns a nonce-less node → raises, no click;
   DESTRUCTIVE post-approval host divergence → aborts.
10. **MCP fail-closed:** with `BROWSER_AGENT_AUTO_APPROVE=true` and no elicitation, a
    DESTRUCTIVE call → denied. ✅ (already covered by `tests/test_mcp.py`).
11. **Sub-agent surfacing:** depth-1 destructive → `_SubAgentBlocked` → orchestrator
    re-issues under `ApprovalRequired` (not a `gather`-swallowed line).
12. **Payment iframe:** click inside a cross-origin `checkout.stripe.com` iframe →
    DESTRUCTIVE via `src`-host escalator, one-time-per-checkout.
13. **Seam intact:** extend `tests/test_event_contract.py` — `EXPECTED_VERSION="1.1"`,
    add `"reason"` to `approval_request` keys.
14. **Config propagation:** `Settings.to_core_config()` and SDK constructor both carry
    the new knobs.

## Already done (shipped ahead of the rest)

**Ground-truth classification for DOM + vision (M0 core).** The classifier no longer
reads an opaque ref or a model label:

- `session.describe_target(ref=… | x,y=… | active=…)` resolves the **real** element
  (DOM by `data-ref`, vision by `document.elementFromPoint` climbing to the nearest
  interactive ancestor) and returns `{found, interactive, name, role, tag, href,
  input_type, in_form}`.
- `agent._classify` is replaced by `_risk_for(name, input_type, kind, text)` +
  `_max_risk`. `act` classifies on the resolved element name (not the opaque ref);
  `click_at` classifies on the hit-tested element under (x, y) with the model's `label`
  as an **escalator only** (it can raise risk, never lower it); `type_at` classifies on
  the resolved field + typed text.
- Lexicon expanded (place order / purchase / transfer / wire / withdraw / unsubscribe /
  deactivate / …) with multi-word phrases so bare "order"/"confirm"/"remove" don't
  over-trigger, plus an **escape-hatch** set (Cancel/Close/No thanks → never destructive).
- **Live-verified on real Chromium** (a checkout page: "Place Order — $128.40" → gated on
  both paths; Cancel / Remove from cart / Add to cart → not gated; password field →
  sensitive). Tests: `tests/test_approval_gate.py` (unit) and
  `tests/test_sdk.py::test_e2e_unlabeled_coordinate_click_still_gates` (a no-label
  coordinate click on a real destructive button pauses for approval).
- **Known residual still open:** an icon-only button with no accessible name clicked via
  DOM `act` (no label on that path) resolves `name=""` → SAFE. Closing this needs the
  payment-host fail-closed rule (below). `press_key`/`drag`/`navigate` are not yet routed
  through ground-truth, and there is no TOCTOU re-check yet.

**MCP `AUTO_APPROVE` fail-open** — the red-team's highest-severity finding, a **current**
bug fixed independently of the larger redesign:

- **MCP `BROWSER_AGENT_AUTO_APPROVE` fail-open.** In `_make_approver`, when the host
  couldn't elicit a human decision, this env var auto-*allowed* the action. Since the gate
  only ever pauses on DESTRUCTIVE and `ApprovalRequest` carries no risk field, every
  request reaching that handler is destructive — so a single env var turned the entire
  gate into a no-op on the MCP surface. **Fixed:** the elicitation-fallback now always
  denies; destructive actions are never auto-approved. The env var is removed from the
  code, docstring, and `docs/mcp.md`; `tests/test_mcp.py` asserts the flag can no longer
  approve a destructive call.

## Residual limitations (NOT solved by this plan)

Honest about what GroundGate does **not** cover:

1. **Handler-rebind on a persistent DOM node** — a React-stable node whose `onClick` is
   rebound while tag/text/host/amount stay identical defeats identity- and facts-based
   checks. Behavior-level verification is out of scope.
2. **Opaque/tokenized GET side-effects** — a bare opaque GET to a first-party merchant
   host with no token pattern still rides through as a SAFE navigate.
3. **DPR drift on fingerprint-hardened Browserbase** — a `deviceScaleFactor ≠ 1` could
   shift `elementFromPoint` to a neighboring node; we fail closed on null, not on a
   plausible-but-wrong sibling. Mitigated, not eliminated.
4. **Global/document-level keydown submit with no focusable control** on a *non-payment*
   first-party host — nothing for `hit_test` to read; only payment/auth hosts fail closed.
5. **First-party final-confirm with a generic label and no amount/verb/form** — a `<div>`
   "Confirm" on `shop.acme.com`, amount three DOM nodes away, not in a `<form>` → SENSITIVE
   at best.
6. **Slide-to-pay on a non-interactive rail** — drag endpoints resolve a background rail;
   the non-interactive-background-stays-SAFE rule lets it through.
7. **Cross-origin payment-iframe over-gate UX** — one-time-per-checkout is heuristic; a
   multi-step 3DS flow may pause more than once.
8. **`go_back`/`go_forward` re-firing a pushState checkout side-effect** — left SAFE.
9. **Sub-agent navigating to a destructive deep-link** — caught by the URL escalators
   only, same hole as (2) from a different door.
