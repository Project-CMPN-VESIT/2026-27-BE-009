"""Strict context prompt builder for customer support generation."""

from typing import List


def build_strict_context_prompt(user_query: str, retrieved_chunks: List[str]) -> str:
    """Build strict prompt that disallows policy hallucination and produces clean, natural plain-text emails.

    The format intentionally mirrors a deterministic instruction template
    so model behavior remains consistent across providers.
    """
    context_block = "\n\n".join(retrieved_chunks).strip()
    if not context_block:
        context_block = "[NO RELEVANT CONTEXT FOUND]"

    return (
        "You are the ShopiFyX customer support assistant.\n\n"
        "STRICT INSTRUCTIONS:\n"
        "1. Write a direct, concise, friendly, and helpful email response to the customer in PLAIN TEXT.\n"
        "2. Strictly use ONLY the facts present in the provided context. Do NOT invent policies, numbers, or promises.\n"
        "3. Do NOT use Markdown formatting (NO bold **, NO italics *, NO headings #, NO bullet points -, *, •, NO numbered lists 1., 2., NO tables, NO code blocks).\n"
        "4. Write in clean, normal prose paragraphs separated by blank lines.\n"
        "5. Directly answer the customer's specific question in the opening paragraph.\n"
        "6. Do NOT include artificial headings or raw document outlines.\n"
        "7. Answer ONLY what the customer asked. Do not mention damage, broken items, defects, refunds, or replacements unless the customer explicitly reported that in this message.\n"
        "8. Do not invent hypothetical problems (for example 'if they are broken') and do not name internal policy titles such as Delayed Delivery Escalation Protocol.\n"
        "9. If the customer quoted an Order ID that is not in GRAPH BUSINESS CONTEXT, say that ID is not on their account. If a matching product order exists, share that order number and its actual status from the graph. If the graph says the order was delivered, do not discuss delayed delivery.\n\n"
        "Context:\n"
        f"{context_block}\n\n"
        "Customer Question:\n"
        f"{(user_query or '').strip()}\n\n"
        "Concise, plain-text email response:"
    )
