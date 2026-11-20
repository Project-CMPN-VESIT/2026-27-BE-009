"""Main multi-agent orchestration flow for customer support emails (SalesAI V3).

Integrates the persistent Customer Memory Agent into the email intelligence pipeline
with strict dependency injection, stage-by-stage structured logging, and verified flow ordering.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from app.agents.decision import build_final_email, decide_email_action, validate_email_response
from app.agents.escalation import escalate_to_human
from app.agents.generator import (
    generate_reply,
    normalize_customer_response,
    sanitize_customer_reply,
    strip_unsolicited_issue_claims,
)
from app.agents.strategy import select_strategy
from app.config import settings
from app.db.customer_memory import has_received_return, normalize_email
from app.db.supabase_client import log_interaction, save_email_record
from app.email.safety_middleware import enforce_email_safety
from app.email.send_email import extract_customer_name, send_email, send_email_reply
from app.memory.customer_memory import CustomerMemoryAgent, memory_agent as default_memory_agent
from app.memory.memory_models import CustomerMemory, CustomerProfile, FormattedMemoryContext
from app.nlp.dual_llm import select_best_nlp_output
from app.nlp.emotion import detect_emotion
from app.nlp.intent import classify_intent
from app.nlp.preprocess import clean_query_text, preprocess_text
from app.rag.prompt_builder import build_strict_context_prompt
from app.rag.query_guard import QueryClass, inspect_query
from app.rag.response_validator import SAFE_FALLBACK_RESPONSE, validate_response
from app.rag.retrieval import retrieve_relevant_chunks, retrieve_similar_user_messages

LOGGER = logging.getLogger(__name__)


def _region_from_address(address: str) -> str:
    if not address:
        return ""
    return str(address).split(",")[0].strip()


def _resolve_delivery_region(orders_list: Optional[List[Dict[str, Any]]], order_num: str) -> str:
    wanted = (order_num or "").strip().upper()
    fallback = ""
    for order in orders_list or []:
        addr = _region_from_address(order.get("shipping_address") or "")
        if not addr:
            continue
        onum = str(order.get("order_number") or "").upper()
        if wanted and onum == wanted:
            return addr
        if not fallback:
            fallback = addr
    return fallback


def _same_day_pickup_line(region: str) -> str:
    clean = (region or "").strip()
    if clean:
        return f"A ShopiFyX courier will pick up the item from your {clean.title()} address today."
    return "A ShopiFyX courier will pick up the item from your delivery address today."


_ORDER_ID_RE = re.compile(r"\b(?:ORD|SFX)[-_][A-Za-z0-9-]+\b", re.I)
_PRODUCT_MATCH_STOP = {
    "shopifyx", "order", "product", "item", "the", "and", "for", "with",
}


def _extract_mentioned_order_ids(text: str) -> List[str]:
    found: List[str] = []
    seen = set()
    for match in _ORDER_ID_RE.finditer(text or ""):
        token = match.group(0).upper().replace("_", "-")
        if token not in seen:
            seen.add(token)
            found.append(token)
    return found


def _format_graph_date(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            pass
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    return text


def _product_names(order: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for product in order.get("products") or []:
        name = str(product.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _order_status_clause(order: Dict[str, Any]) -> str:
    shipment = order.get("shipment") or {}
    delivered_at = _format_graph_date(shipment.get("delivered_at"))
    ship_status = str(shipment.get("status") or "").lower()
    order_status = str(order.get("status") or "").lower()
    if delivered_at or "deliver" in ship_status or order_status == "delivered":
        when = f" on {delivered_at}" if delivered_at else ""
        return f"which was delivered{when}"
    status = order_status or ship_status
    if status:
        return f"which is currently {status.replace('_', ' ')}"
    return "which is on your account"


def _message_matches_product(message: str, product_name: str) -> bool:
    msg = (message or "").lower()
    name = (product_name or "").lower()
    if not name or not msg:
        return False
    if name in msg:
        return True
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", name)
        if token not in _PRODUCT_MATCH_STOP and len(token) >= 3
    ]
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in msg)
    return hits >= 2 or (len(tokens) == 1 and hits == 1)


def _related_orders_for_message(message: str, orders: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    related: List[Dict[str, Any]] = []
    for order in orders or []:
        if any(_message_matches_product(message, name) for name in _product_names(order)):
            related.append(order)
    return related


def _build_unknown_order_id_reply(
    customer_name: str,
    customer_message: str,
    orders: Optional[List[Dict[str, Any]]],
) -> str:
    """Correct a quoted order ID that is not on the customer's graph account."""
    mentioned = _extract_mentioned_order_ids(customer_message)
    if not mentioned:
        return ""
    known = {
        str(order.get("order_number") or "").upper()
        for order in (orders or [])
        if order.get("order_number")
    }
    unknown = [order_id for order_id in mentioned if order_id not in known]
    if not unknown:
        return ""

    quoted = unknown[0]
    related = _related_orders_for_message(customer_message, orders)
    if not related and orders and len(orders) == 1:
        related = list(orders[:1])

    first_name = (customer_name or "").strip().split()[0].title() if (customer_name or "").strip() else ""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    parts = [
        greeting,
        "",
        f"I checked our records for order {quoted} and that order ID is not on your account.",
    ]
    if related:
        order = related[0]
        names = _product_names(order)
        product_label = names[0] if names else "order"
        order_num = str(order.get("order_number") or "")
        only = "The only" if len(related) == 1 else "A matching"
        parts.extend([
            "",
            f"{only} {product_label} order we have is {order_num}, {_order_status_clause(order)}.",
            "",
            "Please confirm if that is the order you meant, or share the correct order ID. "
            "Tell us what you need help with on that order and we will take it from there.",
        ])
    else:
        recent = [
            str(order.get("order_number") or "")
            for order in (orders or [])[:3]
            if order.get("order_number")
        ]
        if recent:
            parts.extend([
                "",
                f"The orders on your account include {', '.join(recent)}. "
                "Please confirm which order you meant, or share the correct order ID, and tell us what you need help with.",
            ])
        else:
            parts.extend([
                "",
                "Please double-check the order ID, or share the product name and order date, "
                "and tell us what you need help with.",
            ])
    parts.extend(["", "Best regards,", "Customer Support Team", "ShopiFyX"])
    return "\n".join(parts)


def _generate_validated_reply(
    current_message: str,
    intent: str,
    emotion: str,
    strategy: str,
    kb_context: List[str],
    customer_memory: Optional[CustomerMemory] = None,
    reply_memory: Optional[List[str]] = None,
    customer_name: str = "",
) -> tuple[str, str]:
    # Build prompt chunks including internal KB policy chunks and Graph / Customer Memory facts
    prompt_chunks = list(kb_context)
    if customer_memory:
        graph_text = (getattr(customer_memory, "graph_context_text", "") or "").strip()
        if graph_text:
            prompt_chunks.append(graph_text)
        from app.memory.memory_formatter import format_customer_memory
        formatted_mem = format_customer_memory(customer_memory, current_intent=intent, current_message=current_message)
        if formatted_mem and formatted_mem.full_context_text:
            prompt_chunks.append(formatted_mem.full_context_text)

    strict_prompt = build_strict_context_prompt(user_query=current_message, retrieved_chunks=prompt_chunks)

    reply = generate_reply(
        current_message=current_message,
        intent=intent,
        emotion=emotion,
        strategy=strategy,
        kb_context=kb_context,
        customer_memory=customer_memory,
        reply_memory=reply_memory,
        strict_prompt=strict_prompt,
        customer_name=customer_name,
    )
    cleaned = normalize_customer_response(
        strip_unsolicited_issue_claims(
            normalize_customer_response(reply, customer_name=customer_name),
            current_message,
        ),
        customer_name=customer_name,
    )

    # Validation contexts must include both internal KB policy chunks and Graph / Customer Memory facts
    validation_contexts = list(kb_context)
    if customer_memory:
        graph_text = (getattr(customer_memory, "graph_context_text", "") or "").strip()
        if graph_text:
            validation_contexts.append(graph_text)
        from app.memory.memory_formatter import format_customer_memory
        formatted_mem = format_customer_memory(customer_memory, current_intent=intent, current_message=current_message)
        if formatted_mem and formatted_mem.full_context_text:
            validation_contexts.append(formatted_mem.full_context_text)

    validation = validate_response(answer=cleaned, context_chunks=validation_contexts)
    if validation.is_valid:
        return cleaned, "validated"

    # Retry generation with explicit factual grounding reinforcement
    retry_prompt = (
        strict_prompt
        + "\n\nIMPORTANT: The previous answer failed factual or formatting validation. Output pure plain text, strictly following internal policy and graph context facts with no markdown formatting."
    )
    retry_reply = generate_reply(
        current_message=current_message,
        intent=intent,
        emotion=emotion,
        strategy=strategy,
        kb_context=kb_context,
        customer_memory=customer_memory,
        reply_memory=reply_memory,
        strict_prompt=retry_prompt,
        customer_name=customer_name,
    )
    retry_clean = normalize_customer_response(
        strip_unsolicited_issue_claims(
            normalize_customer_response(retry_reply, customer_name=customer_name),
            current_message,
        ),
        customer_name=customer_name,
    )
    retry_validation = validate_response(answer=retry_clean, context_chunks=validation_contexts)
    if retry_validation.is_valid:
        return retry_clean, "validated_retry"
    return normalize_customer_response(SAFE_FALLBACK_RESPONSE, customer_name=customer_name), "fallback"


def _calculate_confidence(context_docs: List[str], reply_memory: List[str], reply: str, is_conversational: bool = False) -> float:
    """Calculate overall pipeline confidence score."""
    if is_conversational:
        return 0.95
    confidence = 0.5
    if context_docs and len(context_docs) > 0:
        confidence += 0.15
    if len(context_docs) > 1:
        confidence += 0.10
    if reply_memory and len(reply_memory) > 0:
        confidence += 0.10
    if len(reply_memory) > 1:
        confidence += 0.05
    reply_text = (reply or "").strip()
    if len(reply_text) > 50:
        confidence += 0.10
    if len(reply_text) > 200:
        confidence += 0.05
    if len(reply_text) < 20:
        confidence -= 0.20
    return max(0.0, min(1.0, confidence))


def handle_customer_email(
    customer_email: str,
    subject: str,
    body: str,
    email_id: str = "",
    memory_service: Optional[CustomerMemoryAgent] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Execute the complete 16-stage V3 pipeline with persistent Customer Memory and Multimodal capability.
    
    1. Receive email
    1B. Visual Analysis (Image attachments)
    2. Normalize email/message
    3. Resolve customer (CustomerProfile)
    4. Retrieve customer memory (CustomerMemory)
    4B. Order-Visual Consistency Guardrail Check
    5. Detect intent
    6. Detect emotion
    7. Retrieve ShopiFyX KB context
    8. Retrieve relevant previous reply memory
    9. Select response strategy
    10. Generate memory-aware response
    11. Validate response
    12. Make escalation decision
    13. Send email if approved
    14. Extract new customer memory
    15. Update customer memory
    16. Persist final conversation record
    """
    request_id = str(uuid4())[:8]
    if not email_id:
        email_id = request_id

    # Dependency injection: use provided memory service or default singleton
    mem_agent = memory_service or default_memory_agent

    try:
        # Step 1: Receive email
        LOGGER.info("[%s] [Stage 1/16] Received email from: %s | Subject: %s", request_id, customer_email, subject)

        # Step 2: Normalize email & message
        clean_email = normalize_email(customer_email)
        normalized_text = preprocess_text(body)
        customer_name = extract_customer_name(customer_email)
        combined_text = f"Subject: {subject}\nMessage: {normalized_text or body}"
        LOGGER.info("[%s] [Stage 2/16] Normalized email: %s | Message len: %d chars", request_id, clean_email, len(normalized_text))

        # Step 3: Resolve customer
        try:
            profile: CustomerProfile = mem_agent.resolve_customer(clean_email, customer_name)
            customer_id = profile.customer_id
            LOGGER.info("[%s] [Stage 3/16] Resolved customer id=%s | Total interactions=%d", request_id, customer_id, profile.total_interactions)
        except Exception as exc:
            LOGGER.error("[%s] [Stage 3/16] Customer resolution fallback: %s", request_id, exc)
            profile = CustomerProfile(customer_id="0", email=clean_email, name=customer_name)
            customer_id = "0"

        # Step 4: Retrieve customer memory (from SQLite & Neo4j Knowledge Graph)
        try:
            customer_memory: CustomerMemory = mem_agent.retrieve_memory(
                customer_id=customer_id,
                customer_email=clean_email,
                query_text=normalized_text,
            )
        except Exception as exc:
            LOGGER.error("[%s] [Stage 4/16] Memory retrieval fallback: %s", request_id, exc)
            customer_memory = CustomerMemory(profile=profile, risk_level="LOW", is_empty=True)

        # Step 4B: Multimodal Visual Analysis & Knowledge Graph Order-Catalog Grounding
        visual_context = None
        orders_list = (customer_memory.graph_context or {}).get("orders") or []

        if attachments:
            LOGGER.info("[%s] [Stage 4B/16] Analyzing %d email attachment(s) with Knowledge Graph orders context", request_id, len(attachments))
            from app.agents.vision_agent import analyze_email_images
            visual_context = analyze_email_images(
                images=attachments,
                customer_message=combined_text,
                customer_orders=orders_list,
            )
            customer_memory.visual_context = visual_context
            LOGGER.info(
                "[%s] [Stage 4B/16] Visual Defect Triage: product='%s' sku='%s' cond='%s' defect='%s' severity='%s' DAR=%.2f conf=%.2f match_order=%s order_num='%s'",
                request_id,
                visual_context.detected_product_name,
                visual_context.matched_catalog_sku,
                visual_context.visual_condition,
                visual_context.defect_type,
                visual_context.severity_level,
                visual_context.defect_area_ratio or 0.0,
                visual_context.visual_confidence,
                visual_context.matches_order_history,
                visual_context.matched_order_number,
            )

        LOGGER.info(
            "[%s] [Stage 4/16] Customer memory retrieved | Risk=%s | Open issues=%d | Turns=%d | Interests=%d | Visual=%s",
            request_id,
            customer_memory.risk_level,
            len(customer_memory.open_issues),
            len(customer_memory.recent_conversations),
            len(customer_memory.interests),
            bool(customer_memory.visual_context and customer_memory.visual_context.has_images),
        )

        # Step 4C: Order-Visual Consistency Guardrail Check
        from app.rag.query_guard import verify_visual_order_consistency
        visual_guard = verify_visual_order_consistency(
            visual_context=customer_memory.visual_context if customer_memory else None,
            customer_orders=orders_list,
            customer_name=customer_name,
            customer_message=combined_text,
        )

        # Step 5 & 6: Memory-aware Intent and Emotion Detection
        nlp_result = select_best_nlp_output(
            text=normalized_text,
            customer_memory=customer_memory,
        )
        intent = str(nlp_result.get("intent", "general_support")).strip()
        intent_confidence = float(nlp_result.get("intent_confidence", 0.5) or 0.5)
        emotion = str(nlp_result.get("emotion", "neutral")).strip()
        emotion_confidence = float(nlp_result.get("emotion_confidence", 0.5) or 0.5)
        reasoning_summary = str(nlp_result.get("reasoning_summary", "")).strip()
        memory_used = bool(nlp_result.get("memory_used", False))
        selected_model = str(nlp_result.get("selected_model", "unknown"))
        LOGGER.info(
            "[%s] [Stage 5-6/16] Detected intent: %s (%.2f) | emotion: %s (%.2f) | memory_used: %s | reason: %s",
            request_id,
            intent,
            intent_confidence,
            emotion,
            emotion_confidence,
            memory_used,
            reasoning_summary,
        )

        # Step 7: Retrieve ShopiFyX KB context (independent from customer memory)
        clean_intent = (intent or "").strip().lower()
        guard_result = visual_guard or inspect_query(normalized_text)
        is_conversational = (
            clean_intent in {"thanks", "greeting", "conversational", "acknowledgement"}
            or guard_result.classification == QueryClass.CONVERSATIONAL
        )
        is_guard_deflected = guard_result.classification in {
            QueryClass.SUSPICIOUS,
            QueryClass.OFF_TOPIC,
            QueryClass.GIBBERISH,
            QueryClass.ORDER_ITEM_MISMATCH,
        }

        if is_conversational or is_guard_deflected:
            LOGGER.info(
                "[%s] [Stage 7/16] Skipped RAG retrieval: is_conversational=%s, guard=%s",
                request_id,
                is_conversational,
                guard_result.classification.value,
            )
            kb_context = []
        else:
            retrieval_query = clean_query_text(body)
            query = f"subject: {subject}\nmessage: {retrieval_query or normalized_text}"
            retrieval_result = retrieve_relevant_chunks(
                query=query,
                top_k=settings.rag_top_k,
                min_similarity=settings.rag_similarity_threshold,
                relaxed_fallback_k=settings.rag_relaxed_fallback_k,
                use_keyword_boost=settings.rag_keyword_boost,
            )
            retrieved_chunks = retrieval_result.chunks
            kb_context = [chunk.to_context_block() for chunk in retrieved_chunks]
            LOGGER.info("[%s] [Stage 7/16] Retrieved %d internal KB policy chunks", request_id, len(kb_context))

        # Step 8: Retrieve relevant previous reply memory
        similar_user_messages = retrieve_similar_user_messages(query=f"subject: {subject}\nmessage: {normalized_text}", k=2) if not (is_conversational or is_guard_deflected) else []
        reply_memory: List[str] = (customer_memory.previous_replies or []) + similar_user_messages
        LOGGER.info("[%s] [Stage 8/16] Assembled %d relevant previous reply/interaction patterns", request_id, len(reply_memory))

        # Step 9: Select response strategy (memory & risk aware)
        strategy = select_strategy(intent=intent, emotion=emotion, customer_memory=customer_memory)
        LOGGER.info("[%s] [Stage 9/16] Selected response strategy: %s", request_id, strategy)

        # Step 9B: Inventory & Autonomous Action Dispatcher
        action_context: Dict[str, Any] = {}
        has_attachment = bool(
            attachments
            and customer_memory.visual_context
            and customer_memory.visual_context.has_images
        )
        vc = customer_memory.visual_context if has_attachment else None
        dar_threshold = float(getattr(settings, "vision_dar_auto_threshold", 0.15) or 0.15)
        conf_threshold = float(getattr(settings, "vision_confidence_auto_threshold", 0.70) or 0.70)
        vision_ok = False
        dar_val = 0.0
        conf_val = 0.0
        defect_signal = False
        is_visual_defect_confirmed = False

        if has_attachment:
            # Direct visual decision only when Groq vision JSON is valid AND both
            # DAR and confidence meet the published auto-decision thresholds.
            vision_ok = bool(vc and getattr(vc, "vision_available", True) and getattr(vc, "json_valid", True))
            dar_val = float(vc.defect_area_ratio or 0.0) if vc else 0.0
            conf_val = float(vc.visual_confidence or 0.0) if vc else 0.0
            defect_signal = bool(
                vc
                and vision_ok
                and vc.visual_condition in {"damaged", "torn", "defective", "wrong_item"}
                and (vc.defect_type or "").lower() not in {"none", "unclear", ""}
            )
            is_visual_defect_confirmed = bool(
                defect_signal and dar_val >= dar_threshold and conf_val >= conf_threshold
            )
            LOGGER.info(
                "[%s] [Stage 9B/16] Vision gate: vision_ok=%s defect=%s DAR=%.2f (auto>=%.2f) conf=%.2f (auto>=%.2f) -> %s",
                request_id,
                vision_ok,
                defect_signal,
                dar_val,
                dar_threshold,
                conf_val,
                conf_threshold,
                "AUTO_DECISION" if is_visual_defect_confirmed else "HUMAN_ESCALATION",
            )
        else:
            LOGGER.info(
                "[%s] [Stage 9B/16] No image attached; skipping vision. Answering with LLM + RAG + knowledge graph.",
                request_id,
            )

        if not is_guard_deflected:
            from app.db.customer_memory import create_or_update_customer_issue
            extracted_order = re.search(r"\b(ORD-[A-Za-z0-9-]+)\b", f"{subject} {normalized_text}", re.I)

            # CASE 1: Attachment provided, but NO defect found / DAR is 0.00 / defect is none or unclear
            if has_attachment and not is_visual_defect_confirmed:
                target_sku = (vc.matched_catalog_sku if vc else None) or ""
                target_name = (vc.detected_product_name if vc else None) or "attached product"
                order_num = (
                    (vc.matched_order_number if vc and vc.matched_order_number else None)
                    or (extracted_order.group(1).upper() if extracted_order else None)
                    or (orders_list[0].get("order_number") if orders_list else "")
                )

                defect_val = vc.defect_type if vc else "none"
                vision_failed = bool(vc and not getattr(vc, "vision_available", True))
                if vision_failed:
                    review_reason = "Vision analysis was unavailable for the attached image."
                elif defect_signal and dar_val < dar_threshold:
                    review_reason = (
                        f"Visible defect is below the auto-decision DAR threshold "
                        f"(DAR {dar_val:.2f} < {dar_threshold:.2f})."
                    )
                elif defect_signal and conf_val < conf_threshold:
                    review_reason = (
                        f"Vision confidence is below the auto-decision threshold "
                        f"(confidence {conf_val:.2f} < {conf_threshold:.2f})."
                    )
                else:
                    review_reason = (
                        f"No visible physical defect detected (DAR: {dar_val:.2f}, defect: {defect_val})."
                    )

                LOGGER.info(
                    "[%s] [Stage 9B/16] Visual triage did not confirm a defect (DAR=%.2f, defect='%s', vision_ok=%s) for '%s'. Forwarding to human support.",
                    request_id,
                    dar_val or 0.0,
                    defect_val,
                    vision_ok,
                    target_name,
                )

                create_or_update_customer_issue(
                    customer_id=customer_id,
                    issue_title=f"Manual Support Review: {target_name}",
                    description=f"Attachment received for Order {order_num or 'unknown'}. {review_reason} Forwarding to human support team for manual review.",
                    status="under_review",
                    priority="medium",
                    resolution_notes=review_reason + " Forwarded to human support specialist for manual review.",
                    order_number=order_num,
                    defect_type=defect_val,
                    severity=vc.severity_level if vc else "none",
                    defect_area_ratio=dar_val,
                    suggested_action="Manual Review / Human Support Escalation",
                    agent_decision="escalate_no_defect",
                )

                action_context["action_taken"] = "no_defect_detected_escalated"
                action_context["target_name"] = target_name
                action_context["order_num"] = order_num
                action_context["defect_area_ratio"] = dar_val

            # CASE 2: Visual defect is confirmed OR explicit refund/exchange requested without attachment
            elif is_visual_defect_confirmed or (not has_attachment and clean_intent in {"refund_request", "refund", "return_request"}):
                from app.tools.inventory_tool import check_inventory_availability, execute_refund_action, execute_exchange_action

                target_sku = (vc.matched_catalog_sku if vc else None) or ""
                target_name = (vc.detected_product_name if vc else None) or "Product"
                order_num = (
                    (vc.matched_order_number if vc and vc.matched_order_number else None)
                    or (extracted_order.group(1).upper() if extracted_order else None)
                    or (orders_list[0].get("order_number") if orders_list else "")
                )

                inv_result = check_inventory_availability(sku=target_sku, product_name=target_name)
                stock_available = inv_result.in_stock
                units = inv_result.available_units

                LOGGER.info(
                    "[%s] [Stage 9B/16] Inventory & Action Dispatcher: SKU='%s' in_stock=%s (%d units) | Order='%s'",
                    request_id,
                    inv_result.sku,
                    stock_available,
                    units,
                    order_num,
                )

                # Check if user explicitly demanded/confirmed refund
                refund_keywords = {"refund", "money back", "return money", "cancel and refund", "want refund", "prefer refund"}
                wants_refund = any(kw in normalized_text.lower() for kw in refund_keywords) or clean_intent in {"refund_request", "refund"}

                # Check if user explicitly demanded replacement
                exchange_keywords = {"replacement", "exchange", "send new", "replace it", "swap"}
                wants_exchange = any(kw in normalized_text.lower() for kw in exchange_keywords) and not wants_refund

                if wants_refund:
                    if has_received_return(customer_id=customer_id, order_number=order_num):
                        ref_res = execute_refund_action(
                            order_number=order_num,
                            customer_email=clean_email,
                            customer_id=customer_id,
                            reason=f"Returned product received: {vc.defect_type if vc else 'Item issue'}",
                        )
                        action_context["action_taken"] = "refund_initiated"
                        action_context["refund_id"] = ref_res["refund_id"]
                    else:
                        create_or_update_customer_issue(
                            customer_id=customer_id,
                            issue_title=f"Refund Request ({order_num})",
                            description=f"Refund requested for Order {order_num}; returned product has not been marked received.",
                            status="open",
                            priority="high",
                            resolution_notes="Refund is pending receipt of the returned product.",
                            order_number=order_num,
                            defect_type=vc.defect_type if vc else "damaged",
                            severity=vc.severity_level if vc else "moderate",
                            defect_area_ratio=vc.defect_area_ratio if vc else None,
                            suggested_action="Await Returned Product",
                            agent_decision="hold_refund_until_received",
                        )
                        action_context["action_taken"] = "refund_pending_return"
                    action_context["target_name"] = target_name
                    action_context["order_num"] = order_num
                    action_context["delivery_region"] = _resolve_delivery_region(orders_list, order_num)
                    if action_context["action_taken"] == "refund_initiated":
                        LOGGER.info("[%s] [Stage 9B/16] Autonomous Action Executed: %s (Ref: %s)", request_id, ref_res["status"], ref_res["refund_id"])
                    else:
                        LOGGER.info("[%s] [Stage 9B/16] Refund held until returned product is marked received", request_id)
                elif wants_exchange and stock_available:
                    ex_res = execute_exchange_action(
                        order_number=order_num,
                        customer_email=clean_email,
                        sku=target_sku,
                        replacement_sku=target_sku,
                        customer_id=customer_id,
                    )
                    action_context["action_taken"] = "exchange_pending"
                    action_context["exchange_id"] = ex_res["exchange_id"]
                    action_context["target_name"] = target_name
                    action_context["order_num"] = order_num
                    LOGGER.info("[%s] [Stage 9B/16] Autonomous Action Executed: %s (Ex: %s)", request_id, ex_res["status"], ex_res["exchange_id"])
                elif is_visual_defect_confirmed:
                    suggested_opt = "Full Refund or Instant Replacement" if stock_available else "Full Refund or Catalog Alternative"
                    create_or_update_customer_issue(
                        customer_id=customer_id,
                        issue_title=f"Defective Item: {target_name}",
                        description=f"Defect reported for Order {order_num}. {vc.diagnostic_reasoning if vc else 'Damage inspected.'}",
                        status="options_presented",
                        priority="high",
                        resolution_notes=f"Action options presented to customer. Replacement stock available={stock_available} ({units} units).",
                        order_number=order_num,
                        defect_type=vc.defect_type if vc else "damaged",
                        severity=vc.severity_level if vc else "moderate_functional",
                        defect_area_ratio=vc.defect_area_ratio if vc else 0.25,
                        suggested_action=suggested_opt,
                        agent_decision="options_presented",
                    )
                    action_context["action_taken"] = "options_presented"
                    action_context["stock_available"] = stock_available
                    action_context["available_units"] = units
                    action_context["alternatives"] = inv_result.alternative_products
                    action_context["target_name"] = target_name
                    action_context["order_num"] = order_num
                    LOGGER.info("[%s] [Stage 9B/16] Customer Issue Recorded in Supabase | Status=options_presented | SuggestedAction='%s'", request_id, suggested_opt)

        # Step 10: Generate memory-aware response
        if is_guard_deflected:
            generated_reply = guard_result.suggested_reply or normalize_customer_response(SAFE_FALLBACK_RESPONSE, customer_name=customer_name)
            gen_status = f"guard_{guard_result.classification.value}"
            LOGGER.info("[%s] [Stage 10/16] Used QueryGuard suggested reply (%s) | len: %d chars", request_id, gen_status, len(generated_reply))
        elif is_conversational:
            closing = f"Hi {customer_name},\n\n" if customer_name and customer_name.strip() else "Hi,\n\n"
            generated_reply = (
                f"{closing}"
                "You're very welcome! If you have any further questions or need additional assistance with your order, "
                "please feel free to reach out. Have a wonderful day!\n\n"
                "Best regards,\n"
                "Customer Support Team\n"
                "ShopiFyX"
            )
            gen_status = "conversational_direct"
            LOGGER.info("[%s] [Stage 10/16] Generated conversational direct reply | len: %d chars", request_id, len(generated_reply))
        elif action_context.get("action_taken") == "refund_initiated":
            greeting = f"Hi {customer_name},\n\n" if customer_name and customer_name.strip() else "Hi,\n\n"
            target_name = action_context.get("target_name") or "returned product"
            generated_reply = (
                f"{greeting}"
                f"Thank you for contacting ShopiFyX support regarding Order {action_context.get('order_num')}.\n\n"
                f"We have generated and confirmed the refund for your {target_name} on this order. "
                "The amount is the published ShopiFyX policy allowance for that SKU, not the original order basket total.\n\n"
                f"Your Refund ID is: {action_context.get('refund_id')}\n"
                "This refund has been generated to your original payment method.\n\n"
                "Please let us know if you need any further assistance.\n\n"
                "Best regards,\n"
                "Customer Support Team\n"
                "ShopiFyX"
            )
            gen_status = "action_refund_initiated"
            LOGGER.info("[%s] [Stage 10/16] Generated automated refund confirmation reply | len: %d chars", request_id, len(generated_reply))
        elif action_context.get("action_taken") == "refund_pending_return":
            greeting = f"Hi {customer_name},\n\n" if customer_name and customer_name.strip() else "Hi,\n\n"
            target_name = action_context.get("target_name") or "returned product"
            order_num = action_context.get("order_num") or "your order"
            region = action_context.get("delivery_region") or _resolve_delivery_region(orders_list, order_num)
            pickup_line = _same_day_pickup_line(region)
            generated_reply = (
                f"{greeting}"
                f"Thank you for writing about your {target_name} on order {order_num}. We have logged your refund request.\n\n"
                f"{pickup_line} Please keep the {target_name} ready in its original packaging.\n\n"
                "Once it is collected, we will generate the policy refund to your original payment method. "
                "You will receive a second email with the refund amount, refund ID, and transaction ID.\n\n"
                "Best regards,\n"
                "Customer Support Team\n"
                "ShopiFyX"
            )
            gen_status = "action_refund_pending_return"
            LOGGER.info("[%s] [Stage 10/16] Generated refund-pending-return reply | len: %d chars", request_id, len(generated_reply))
        elif action_context.get("action_taken") == "no_defect_detected_escalated":
            greeting = f"Hi {customer_name},\n\n" if customer_name and customer_name.strip() else "Hi,\n\n"
            tname = action_context.get("target_name") or "product"
            onum = action_context.get("order_num") or "your order"

            generated_reply = (
                f"{greeting}"
                f"Thank you for contacting ShopiFyX customer support regarding your {tname} (Order {onum}).\n\n"
                f"We have reviewed the photo attachment you provided. Based on our initial visual assessment, no visible physical defect or damage was detected from the image.\n\n"
                f"To ensure your concern is thoroughly and fairly handled, we have forwarded your request directly to our human customer support team for manual review and assistance. "
                f"A support specialist will investigate your case and follow up with you shortly.\n\n"
                f"If you have additional photos from different angles, a brief video clip, or more details describing the issue, please feel free to reply directly to this email.\n\n"
                f"Best regards,\n"
                f"Customer Support Team\n"
                f"ShopiFyX"
            )
            gen_status = "action_no_defect_escalated"
            LOGGER.info("[%s] [Stage 10/16] Generated no-defect triage notification reply | len: %d chars", request_id, len(generated_reply))
        elif action_context.get("action_taken") == "options_presented" and customer_memory.visual_context and customer_memory.visual_context.has_images:
            greeting = f"Hi {customer_name},\n\n" if customer_name and customer_name.strip() else "Hi,\n\n"
            tname = action_context.get("target_name") or "product"
            onum = action_context.get("order_num") or "your order"
            stock_avail = action_context.get("stock_available", True)

            if stock_avail:
                options_body = (
                    f"We have reviewed the photo you provided for your {tname} (Order {onum}) and verified the damage. We sincerely apologize for this inconvenience!\n\n"
                    f"Since replacement units are currently in stock, we are pleased to offer you two immediate resolution options:\n\n"
                    f"1. **100% Full Refund**: We will immediately initiate a complete refund to your original payment method.\n"
                    f"2. **Instant Replacement & Free Exchange**: We will dispatch a brand new replacement item to your address with zero shipping fees and provide a prepaid return label.\n\n"
                    f"Please reply with your preferred option (Refund or Replacement), and we will process it right away."
                )
            else:
                alts = action_context.get("alternatives", [])
                alt_text = f" (such as our {alts[0]['name']})" if alts else ""
                options_body = (
                    f"We have reviewed the photo you provided for your {tname} (Order {onum}) and verified the damage. We sincerely apologize for this experience!\n\n"
                    f"Because this exact model is currently sold out in our warehouse, we would like to offer you the following solutions:\n\n"
                    f"1. **100% Full Refund**: Processed immediately back to your original payment method.\n"
                    f"2. **Catalog Alternative + 15% Courtesy Credit**: Select an alternative product from our catalog{alt_text} along with an additional 15% courtesy discount credited to your account.\n\n"
                    f"Please reply with your preference and we will execute your choice immediately."
                )

            generated_reply = (
                f"{greeting}"
                f"{options_body}\n\n"
                f"Best regards,\n"
                f"Customer Support Team\n"
                f"ShopiFyX"
            )
            gen_status = "action_options_presented"
            LOGGER.info("[%s] [Stage 10/16] Generated automated resolution options reply | len: %d chars", request_id, len(generated_reply))
        elif (
            unknown_order_reply := _build_unknown_order_id_reply(
                customer_name=customer_name,
                customer_message=f"{subject}\n{normalized_text}",
                orders=orders_list,
            )
        ):
            generated_reply = unknown_order_reply
            gen_status = "unknown_order_id_corrected"
            LOGGER.info(
                "[%s] [Stage 10/16] Quoted order ID is not on the account; sent graph-grounded correction | len: %d chars",
                request_id,
                len(generated_reply),
            )
        elif not kb_context:
            LOGGER.warning("[%s] [Stage 10/16] No KB context found, using safe policy fallback", request_id)
            generated_reply = normalize_customer_response(SAFE_FALLBACK_RESPONSE, customer_name=customer_name)
            gen_status = "safe_fallback"
        else:
            generated_reply, gen_status = _generate_validated_reply(
                current_message=normalized_text,
                intent=intent,
                emotion=emotion,
                strategy=strategy,
                kb_context=kb_context,
                customer_memory=customer_memory,
                reply_memory=reply_memory,
                customer_name=customer_name,
            )
            LOGGER.info("[%s] [Stage 10/16] Generated reply status: %s | len: %d chars", request_id, gen_status, len(generated_reply))
            LOGGER.debug("[%s] [Stage 10/16] Draft Reply Content:\n%s", request_id, generated_reply)

        # Step 11: Validate response & Safety middleware
        validation_contexts = list(kb_context)
        if customer_memory:
            graph_text = (getattr(customer_memory, "graph_context_text", "") or "").strip()
            if graph_text:
                validation_contexts.append(graph_text)
            from app.memory.memory_formatter import format_customer_memory
            formatted_mem = format_customer_memory(customer_memory, current_intent=intent, current_message=normalized_text)
            if formatted_mem and formatted_mem.full_context_text:
                validation_contexts.append(formatted_mem.full_context_text)

        is_action_reply = gen_status.startswith("action_") or gen_status == "unknown_order_id_corrected"
        is_pass_through = (is_conversational or is_guard_deflected or is_action_reply)

        safe_reply, blocked, safety_reason = enforce_email_safety(
            answer=generated_reply,
            retrieved_context_chunks=validation_contexts,
            is_conversational_or_guarded=is_pass_through,
        )
        if blocked and not is_action_reply:
            LOGGER.warning("[%s] [Stage 11/16] Safety middleware enforced: %s", request_id, safety_reason)

        if is_action_reply:
            validation = {"grounded": True, "valid": True, "issues": []}
        else:
            validation = validate_email_response(
                safe_reply,
                validation_contexts,
                intent,
                emotion,
                guard_classification=guard_result.classification.value,
            )

        LOGGER.info(
            "[%s] [Stage 11/16] Response validation grounded=%s valid=%s | Issues=%s",
            request_id,
            validation.get("grounded"),
            validation.get("valid"),
            validation.get("issues") or "none",
        )

        # Step 12: Make escalation decision
        enquiry_intents = {
            "general_support", "inquiry", "product_inquiry", "order_status", "order_tracking",
            "shipping", "delivery", "delivery_status", "tracking", "product_question",
            "availability", "price_inquiry", "where_is_my_order", "catalog",
        }
        is_text_enquiry = (
            not has_attachment
            and not is_action_reply
            and validation.get("valid")
            and clean_intent not in {"refund_request", "refund", "return_request", "complaint"}
        )
        if is_action_reply:
            if gen_status == "action_no_defect_escalated":
                decision = {"decision": "AUTO_SEND", "reason": "no_defect_detected_forwarded_to_human"}
            elif gen_status == "unknown_order_id_corrected":
                decision = {"decision": "AUTO_SEND", "reason": "quoted_order_id_not_on_account"}
            else:
                decision = {"decision": "AUTO_SEND", "reason": "autonomous_action_options_presented"}
        elif is_text_enquiry:
            decision = {
                "decision": "AUTO_SEND",
                "reason": "text_enquiry_answered_with_rag_and_graph",
                "requires_human": False,
            }
        else:
            decision = decide_email_action(
                intent_confidence=intent_confidence,
                emotion_confidence=emotion_confidence,
                validation=validation,
                intent=intent,
                emotion=emotion,
                customer_message=normalized_text,
                generated_response=safe_reply,
                retrieved_context=kb_context,
                customer_risk_level=customer_memory.risk_level,
                customer_memory=customer_memory,
            )
        calculated_conf = _calculate_confidence(kb_context, reply_memory, safe_reply, is_conversational=(is_conversational or is_guard_deflected))
        decision_label = decision.get("decision", "HUMAN_REVIEW")
        LOGGER.info(
            "[%s] [Stage 12/16] Email decision: %s | CustomerRisk=%s | IntentConf=%.2f | Reason: %s",
            request_id,
            decision_label,
            customer_memory.risk_level,
            intent_confidence,
            decision.get("reason"),
        )

        # Step 13: Send email if approved (or trigger escalation)
        status = "failed"
        escalation_reason = ""

        if decision_label == "AUTO_SEND" and validation.get("valid", False):
            send_success = send_email(
                to_email=clean_email,
                subject=subject,
                body=safe_reply,
                use_reply_prefix=True,
                customer_name=customer_name,
            )
            if send_success:
                status = "replied"
                LOGGER.info("[%s] [Stage 13/16] Outbound email sent successfully to %s", request_id, clean_email)
            else:
                status = "failed"
                escalation_reason = "send_failed"
                LOGGER.error("[%s] [Stage 13/16] Outbound email dispatch failed to %s", request_id, clean_email)

            if gen_status == "action_no_defect_escalated":
                try:
                    escalate_to_human(
                        customer_email=clean_email,
                        subject=subject,
                        body=body,
                        reason=f"No visible defect detected (DAR={action_context.get('defect_area_ratio', 0.0):.2f}). Forwarded for manual review.",
                        generated_reply=safe_reply,
                        confidence_score=calculated_conf,
                    )
                    LOGGER.info("[%s] [Stage 13/16] Escalation ticket dispatched to human support team queue", request_id)
                except Exception as esc_err:
                    LOGGER.warning("[%s] [Stage 13/16] Escalation dispatch notice failed: %s", request_id, esc_err)

        elif decision_label in {"HUMAN_REVIEW", "DO_NOT_SEND"}:
            status = "escalated"
            escalation_reason = decision.get("reason", "decision_escalated")
            LOGGER.warning("[%s] [Stage 13/16] Escalating interaction: %s", request_id, escalation_reason)
            try:
                escalate_to_human(
                    customer_email=clean_email,
                    subject=subject,
                    body=body,
                    reason=escalation_reason,
                    generated_reply=safe_reply,
                    confidence_score=calculated_conf,
                )
            except Exception as esc_err:
                LOGGER.warning("[%s] [Stage 13/16] Escalation dispatch notice failed: %s", request_id, esc_err)

        # Steps 14 & 15: Extract new customer memory and update memory state
        LOGGER.info("[%s] [Stage 14-15/16] Updating customer memory (status=%s)", request_id, status)
        try:
            mem_agent.update_memory(
                customer_id=customer_id,
                customer_email=clean_email,
                email_id=email_id,
                subject=subject,
                customer_message=body,
                normalized_message=normalized_text,
                intent=intent,
                intent_confidence=intent_confidence,
                emotion=emotion,
                emotion_confidence=emotion_confidence,
                strategy=strategy,
                reply=safe_reply,
                confidence=calculated_conf,
                status=status,
                escalation_reason=escalation_reason,
                selected_model=selected_model,
                retrieved_context_count=len(kb_context),
                similar_memory_count=len(reply_memory),
            )
            LOGGER.info("[%s] [Stage 15/16] Customer memory update completed", request_id)
        except Exception as mem_err:
            # Crucial: Memory update failure must never crash the pipeline or cause duplicate email dispatch
            LOGGER.error("[%s] [Stage 15/16] Non-blocking memory update error: %s", request_id, mem_err)

        # Step 16: Persist final conversation record & legacy logs
        try:
            save_email_record(
                sender=clean_email,
                subject=subject,
                body=body,
                intent=intent,
                emotion=emotion,
                reply=safe_reply,
                status=status,
                confidence=calculated_conf,
                escalation_reason=escalation_reason,
            )
            log_interaction({
                "customer_email": clean_email,
                "subject": subject,
                "intent": intent,
                "intent_confidence": intent_confidence,
                "emotion": emotion,
                "emotion_confidence": emotion_confidence,
                "strategy": strategy,
                "reply": safe_reply,
                "selected_model": selected_model,
            })
            LOGGER.info("[%s] [Stage 16/16] Interaction and email records persisted successfully", request_id)
        except Exception as log_err:
            LOGGER.warning("[%s] [Stage 16/16] Legacy log recording notice: %s", request_id, log_err)

        return {
            "status": status,
            "decision": decision_label,
            "reply": safe_reply,
            "confidence": f"{calculated_conf:.2f}",
            "intent": intent,
            "emotion": emotion,
            "escalation_reason": escalation_reason,
        }

    except Exception as exc:
        LOGGER.exception("[%s] Unexpected exception in handle_customer_email: %s", request_id, exc)
        return {
            "status": "failed",
            "reply": "",
            "confidence": "0.00",
            "intent": "",
            "emotion": "",
            "escalation_reason": "processing_error",
        }


def process_email(
    email: dict,
    memory_service: Optional[CustomerMemoryAgent] = None,
) -> dict:
    """FastAPI endpoint handler — same multimodal pipeline as Gmail processing."""
    sender = email.get("from") or email.get("sender") or email.get("customer_email") or ""
    subject = email.get("subject", "")
    body = email.get("body", "")
    email_id = email.get("id") or email.get("email_id") or str(uuid4())[:8]
    attachments = email.get("attachments") or None

    if not sender or not subject or not body:
        LOGGER.error("process_email: Missing required email fields")
        return {"status": "failed", "reply": "", "grounded": "False", "human_review_required": "True"}

    result = handle_customer_email(
        customer_email=sender,
        subject=subject,
        body=body,
        email_id=email_id,
        memory_service=memory_service,
        attachments=attachments,
    )
    return {
        "status": result.get("status", "failed"),
        "reply": result.get("reply", ""),
        "intent": result.get("intent", ""),
        "intent_confidence": str(result.get("intent_confidence", result.get("confidence", "0.00"))),
        "emotion": result.get("emotion", ""),
        "emotion_intensity": str(result.get("emotion_intensity", "0.00")),
        "grounded": str(result.get("grounded", result.get("status") == "replied")),
        "email_decision": result.get("decision", ""),
        "human_review_required": str(result.get("status") != "replied"),
        "confidence": result.get("confidence", "0.00"),
        "escalation_reason": result.get("escalation_reason", ""),
    }

