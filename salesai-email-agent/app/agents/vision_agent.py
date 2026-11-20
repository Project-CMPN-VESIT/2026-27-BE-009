"""Vision Agent module for SalesAI multimodal understanding.

Extracts visual information from email image attachments, maps detected items
to the ShopiFyX product catalog, identifies damage/defects, and cross-checks
against placed customer orders for consistency guardrails.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from app.config import settings
from app.memory.memory_models import VisualContext

LOGGER = logging.getLogger(__name__)

# Maximum token budget / dimensions for vision processing
MAX_IMAGE_DIM = 1024
MIN_IMAGE_DIM = 32  # Groq vision rejects images smaller than 32px on either side
MAX_PROMPT_CHARS = 1200


def _extract_json_block(text: str) -> str:
    """Extract first valid JSON object from model output."""
    if not text:
        return ""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start : end + 1]


def _prepare_image_bytes(raw_b64: str) -> Optional[bytes]:
    """Safely decode base64 image data and downscale if PIL is available."""
    if not raw_b64:
        return None

    try:
        clean_b64 = re.sub(r"\s+", "", str(raw_b64)).replace("-", "+").replace("_", "/")
        padding = "=" * (-len(clean_b64) % 4)
        image_bytes = base64.b64decode(clean_b64 + padding)

        # Optional PIL downscaling to respect token budget
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(image_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")

            resized = False
            if img.width < MIN_IMAGE_DIM or img.height < MIN_IMAGE_DIM:
                scale = max(
                    MIN_IMAGE_DIM / max(img.width, 1),
                    MIN_IMAGE_DIM / max(img.height, 1),
                )
                img = img.resize(
                    (max(MIN_IMAGE_DIM, int(img.width * scale)), max(MIN_IMAGE_DIM, int(img.height * scale))),
                    Image.Resampling.NEAREST,
                )
                resized = True

            # Downscale if larger than MAX_IMAGE_DIM
            if img.width > MAX_IMAGE_DIM or img.height > MAX_IMAGE_DIM:
                img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.Resampling.LANCZOS)
                resized = True

            if resized:
                out_io = io.BytesIO()
                img.save(out_io, format="JPEG", quality=85)
                return out_io.getvalue()
        except ImportError:
            pass

        return image_bytes
    except Exception as exc:
        LOGGER.warning("Failed to decode or process image bytes: %s", exc)
        return None


DEFAULT_GROQ_VISION_MODEL = "qwen/qwen3.8-27b"


def _image_data_uri(image_bytes: bytes, mime_type: str) -> str:
    """Encode image bytes as a Groq-compatible data URI."""
    mime = (mime_type or "image/jpeg").split(";")[0].strip().lower() or "image/jpeg"
    if mime == "image/jpg":
        mime = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _call_groq_vision(prompt: str, image_bytes_list: List[bytes], mime_types: List[str]) -> str:
    """Invoke Groq's free multimodal model with real image parts. No Gemini and no text-only fallback."""
    api_key = getattr(settings, "groq_api_key", None) or os.getenv("GROQ_API_KEY", "")
    if not api_key or not image_bytes_list:
        return ""

    if os.getenv("FAULT_VISION_DOWN", "0").lower() in ("1", "true", "yes"):
        LOGGER.warning("FAULT_VISION_DOWN active; failing vision call")
        return ""

    model_name = (
        getattr(settings, "groq_vision_model", "")
        or os.getenv("GROQ_VISION_MODEL", "")
        or DEFAULT_GROQ_VISION_MODEL
    ).strip() or DEFAULT_GROQ_VISION_MODEL

    try:
        from groq import Groq
    except ImportError:
        LOGGER.error("Groq library not installed; vision analysis unavailable")
        return ""

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img_bytes, mime in zip(image_bytes_list[:3], mime_types[:3]):
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _image_data_uri(img_bytes, mime)},
            }
        )

    client = Groq(api_key=api_key)
    messages = [{"role": "user", "content": content}]
    last_error: Optional[Exception] = None

    # Prefer JSON object mode so DAR/severity come back parseable. Older groq SDKs
    # use max_tokens instead of max_completion_tokens.
    request_variants = [
        {"max_completion_tokens": 2048, "response_format": {"type": "json_object"}},
        {"max_tokens": 2048, "response_format": {"type": "json_object"}},
        {"max_completion_tokens": 2048},
        {"max_tokens": 2048},
    ]

    for extra in request_variants:
        try:
            completion = client.chat.completions.create(
                model=model_name,
                temperature=0,
                messages=messages,
                **extra,
            )
            message = completion.choices[0].message if completion.choices else None
            if not message:
                continue
            text = (getattr(message, "content", None) or "").strip()
            if not text:
                text = (getattr(message, "reasoning", None) or "").strip()
            if text:
                served = getattr(completion, "model", "") or model_name
                LOGGER.info("Groq vision model=%s returned %d chars", served, len(text))
                return text
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            LOGGER.warning("Groq vision call on %s failed: %s", model_name, exc)
            break

    if last_error:
        LOGGER.warning("Groq vision unavailable: %s", last_error)
    return ""


def _call_gemini_vision(prompt: str, image_bytes_list: List[bytes], mime_types: List[str]) -> str:
    """Deprecated alias: vision now uses Groq only."""
    return _call_groq_vision(prompt, image_bytes_list, mime_types)


def _normalize_dar(raw: Any, damaged: bool) -> float:
    """Accept 0-1 ratios or 0-100 percents from the vision model."""
    if raw is None or raw == "":
        return 0.30 if damaged else 0.0
    try:
        text = str(raw).strip().replace("%", "")
        number = float(text)
    except (TypeError, ValueError):
        return 0.25 if damaged else 0.0
    if number > 1.0 and number <= 100.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))



def _match_product_with_order_history(
    detected_product_name: str,
    detected_category: str,
    matched_sku: str,
    customer_orders: Optional[List[Dict[str, Any]]],
    customer_message: str = "",
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Cross-verify detected product and customer message against placed orders in Neo4j graph.
    
    Returns: (matches_order, matched_order_number, resolved_product_name, resolved_sku)
    """
    if not customer_orders:
        det_name = (detected_product_name or "").strip()
        return False, None, det_name if det_name else None, None

    det_name_clean = (detected_product_name or "").lower().strip()
    det_sku_clean = (matched_sku or "").lower().strip()
    msg_clean = (customer_message or "").lower()

    generic_stopwords = {"product", "item", "order", "delivery", "package", "parcel", "box", "shipment", "thing", "unknown", "attached", "none"}

    # 1. First check if visual detected SKU or Name directly matches an ordered product
    for order in customer_orders:
        order_num = order.get("order_number") or order.get("id") or "Unknown"
        products = order.get("products") or []
        for prod in products:
            p_name = prod.get("name") or ""
            p_name_lower = p_name.lower()
            p_sku = (prod.get("sku") or "").lower()

            # Direct SKU match
            if det_sku_clean and det_sku_clean != "none" and det_sku_clean == p_sku:
                return True, str(order_num), p_name, prod.get("sku")

            # Non-generic name match
            if det_name_clean and det_name_clean not in generic_stopwords and (det_name_clean in p_name_lower or p_name_lower in det_name_clean):
                return True, str(order_num), p_name, prod.get("sku")

            # Word overlap (1+ distinctive keywords)
            det_words = {w for w in re.findall(r"\b[a-z0-9]{3,}\b", det_name_clean) if w not in generic_stopwords}
            prod_words = {w for w in re.findall(r"\b[a-z0-9]{3,}\b", p_name_lower) if w not in generic_stopwords}
            if det_words and len(det_words.intersection(prod_words)) >= 1:
                return True, str(order_num), p_name, prod.get("sku")

    # 2. If visual detection was generic or uncertain, check if email message text explicitly references an ordered product
    for order in customer_orders:
        order_num = order.get("order_number") or order.get("id") or "Unknown"
        products = order.get("products") or []
        for prod in products:
            p_name = prod.get("name") or ""
            p_name_lower = p_name.lower()
            prod_words = {w for w in re.findall(r"\b[a-z0-9]{4,}\b", p_name_lower) if w not in generic_stopwords}
            
            # Check if distinctive product words appear in customer message (e.g. 'speaker', 'soundmax', 'backpack', 'cabin')
            for pw in prod_words:
                if re.search(r"\b" + re.escape(pw) + r"\b", msg_clean):
                    return True, str(order_num), p_name, prod.get("sku")

    # 3. If customer message or visual explicitly names an un-ordered product (e.g. 'crop top', 't-shirt', 'sandal')
    for un_kw in ["crop top", "croptop", "jeans", "formal shoe", "air fryer", "kettle", "watch", "earbuds"]:
        if un_kw in msg_clean or un_kw in det_name_clean:
            return False, None, un_kw.title(), None

    det_fallback = det_name_clean.title() if (det_name_clean and det_name_clean not in generic_stopwords) else "Attached item"
    return False, None, det_fallback, None



def analyze_email_images(
    images: List[Dict[str, Any]],
    customer_message: str = "",
    customer_orders: Optional[List[Dict[str, Any]]] = None,
) -> VisualContext:
    """Analyze email image attachments using Groq vision grounded in catalog & order context."""
    if not images:
        return VisualContext(has_images=False)

    image_bytes_list: List[bytes] = []
    mime_types: List[str] = []

    for img in images[:3]:  # Max 3 images to bound token budget
        raw_data = img.get("data") or ""
        mime = img.get("mime_type") or "image/jpeg"
        decoded = _prepare_image_bytes(raw_data)
        if decoded:
            image_bytes_list.append(decoded)
            mime_types.append(mime)

    if not image_bytes_list:
        return VisualContext(
            has_images=True,
            image_count=len(images),
            summary="Attached images could not be parsed.",
            visual_condition="unclear",
            defect_type="none",
            severity_level="none",
            defect_area_ratio=0.0,
            vision_available=False,
            json_valid=False,
            matches_order_history=None,
        )

    # Format customer's placed orders into prompt for direct cross-reference
    orders_context_lines = []
    if customer_orders:
        for ord_info in customer_orders[:5]:
            onum = ord_info.get("order_number") or "Order"
            prods = ", ".join([p.get("name", "") for p in ord_info.get("products", []) if p.get("name")])
            orders_context_lines.append(f"- {onum}: {prods}")
    orders_context_str = "\n".join(orders_context_lines) if orders_context_lines else "No placed orders found."

    prompt = (
        "You are the ShopiFyX E-Commerce Vision Analysis Agent.\n\n"
        "TASK:\n"
        "Analyze the provided image attachment(s) from a customer support email.\n\n"
        "CUSTOMER MESSAGE CONTEXT:\n"
        f"\"{customer_message[:300]}\"\n\n"
        "CUSTOMER'S ACTIVE PLACED ORDERS ON FILE:\n"
        f"{orders_context_str}\n\n"
        "SHOPIFYX PRODUCT TAXONOMY:\n"
        "- Electronics & Audio (Bluetooth Speakers, Earbuds, Headphones, Power Banks, Smartwatches, Smart Plugs)\n"
        "- Accessories & Luggage (Cabin Backpacks, Laptop Backpacks, Hard Shell Cabin Cases, Packing Cubes, Tote Bags, Sling Bags)\n"
        "- Fashion & Footwear (Running Shoes, Sneakers, Formal Shoes, Sandals, Jackets, Hoodies, Shirts, Crop Tops, Jeans)\n"
        "- Home & Kitchen (Air Fryers, Electric Kettles, Blender Grinders, Non-Stick Cookware Sets)\n\n"
        "OUTPUT REQUIREMENT:\n"
        "Return ONLY a JSON object with this exact schema:\n"
        "{\n"
        '  "detected_product_name": "<name of product in image, e.g. SoundMax Bluetooth Speaker, Floral Crop Top, AeroStride Running Shoes, or Unknown>",\n'
        '  "detected_category": "<Apparel | Audio | Luggage | Footwear | Kitchenware | Electronics | Receipt | Other>",\n'
        '  "matched_catalog_sku": "<e.g. ELX-002, AL-003, FW-009, or null if not a recognized ShopiFyX product>",\n'
        '  "is_catalog_product": true | false,\n'
        '  "visual_condition": "intact | damaged | torn | wrong_item | defective | unclear",\n'
        '  "defect_type": "<sole_delamination | torn_seam | cracked_grille | cracked_screen | wrong_item | cosmetic_scratch | stain | broken_part | none>",\n'
        '  "severity_level": "<severe_unusable | moderate_functional | minor_cosmetic | none>",\n'
        '  "defect_area_ratio": <float 0.0 to 1.0. Formula: (visible area of the defect) / (visible area of the product in the photo). This is NOT severity. A tiny crack on a large product can be 0.08; a smashed or mostly torn product must be 0.70 to 1.00. Do not default to 0.30-0.40. Examples: faint scratch 0.05-0.12; torn zipper or one strap 0.15-0.35; large tear, crushed face, or damage covering most of the item 0.60-0.90; item almost fully destroyed 0.90-1.00. Never cap at 0.40.>,\n'
        '  "diagnostic_reasoning": "<1-2 sentences technical reasoning describing physical defect location, severity, and cause>",\n'
        '  "defect_description": "<concise description of physical defect, tear, broken part, or none>",\n'
        '  "is_receipt_or_invoice": true | false,\n'
        '  "visual_confidence": <float between 0.0 and 1.0>,\n'
        '  "summary": "<1 sentence factual summary of what the image shows>"\n'
        "}"
    )

    raw_response = _call_groq_vision(prompt, image_bytes_list, mime_types)
    if not raw_response:
        LOGGER.warning("Vision model returned no image analysis; failing honestly instead of guessing from text")

    if raw_response:
        json_str = _extract_json_block(raw_response)
        if json_str:
            try:
                data = json.loads(json_str)
                det_product = data.get("detected_product_name") or "Unknown item"
                det_cat = data.get("detected_category") or "Other"
                sku = data.get("matched_catalog_sku")
                if isinstance(sku, str) and sku.strip().lower() in {"none", "null", "unknown", ""}:
                    sku = None
                is_catalog = bool(data.get("is_catalog_product", False))
                cond = data.get("visual_condition") or "unclear"
                damaged = cond in {"damaged", "torn", "defective", "wrong_item"}
                defect_type = data.get("defect_type") or ("damaged_item" if damaged else "none")
                severity = data.get("severity_level") or ("moderate_functional" if damaged else "none")
                raw_dar = data.get("defect_area_ratio")
                dar = _normalize_dar(raw_dar, damaged=damaged)
                LOGGER.info(
                    "Vision DAR raw=%s normalized=%.2f (%.0f%% of visible product area) damaged=%s",
                    raw_dar,
                    dar,
                    dar * 100,
                    damaged,
                )

                diag_reason = data.get("diagnostic_reasoning") or data.get("defect_description") or ""
                defect = data.get("defect_description") or diag_reason
                is_receipt = bool(data.get("is_receipt_or_invoice", False))
                conf = float(data.get("visual_confidence") or 0.85)
                summary = data.get("summary") or f"Customer provided image of {det_product} ({cond})."

                # Cross verify against customer order history
                matches_order, matched_onum, resolved_pname, resolved_sku = _match_product_with_order_history(
                    detected_product_name=det_product,
                    detected_category=det_cat,
                    matched_sku=sku or "",
                    customer_orders=customer_orders,
                    customer_message=customer_message,
                )

                final_product_name = resolved_pname or det_product
                final_sku = resolved_sku or sku
                if resolved_sku:
                    is_catalog = True

                return VisualContext(
                    has_images=True,
                    image_count=len(image_bytes_list),
                    detected_product_name=final_product_name,
                    detected_category=det_cat,
                    matched_catalog_sku=final_sku,
                    is_catalog_product=is_catalog,
                    visual_condition=cond,
                    defect_type=defect_type,
                    severity_level=severity,
                    defect_area_ratio=dar,
                    diagnostic_reasoning=diag_reason,
                    defect_description=defect,
                    is_receipt_or_invoice=is_receipt,
                    visual_confidence=conf,
                    summary=summary,
                    matches_order_history=matches_order,
                    matched_order_number=matched_onum,
                    vision_available=True,
                    json_valid=True,
                )
            except Exception as exc:
                LOGGER.warning("Failed to parse vision model JSON response: %s", exc)

    LOGGER.warning("Vision analysis unavailable; routing image email to human review without inferred defects")
    return VisualContext(
        has_images=True,
        image_count=len(image_bytes_list),
        detected_product_name="Attached product",
        detected_category="General",
        matched_catalog_sku=None,
        is_catalog_product=False,
        visual_condition="unclear",
        defect_type="none",
        severity_level="none",
        defect_area_ratio=0.0,
        diagnostic_reasoning="",
        defect_description="",
        visual_confidence=0.0,
        summary=f"Customer attached {len(image_bytes_list)} image(s); vision analysis was unavailable.",
        matches_order_history=None,
        matched_order_number=None,
        vision_available=False,
        json_valid=False,
    )
