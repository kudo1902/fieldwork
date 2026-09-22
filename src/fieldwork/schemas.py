"""Target schemas for structured extraction.

Every field is Optional. That is deliberate: the model is instructed to emit
null for anything it cannot read off the page, and a null is a far better
outcome than a confident guess. The strictness knob lives in validation and
review, not in the schema.

To add a document type: define a Pydantic model, then register it in REGISTRY.
Field descriptions are sent to the model as part of the JSON schema, so write
them as instructions to the extractor, not as notes to yourself.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str | None = Field(None, description="Item or service description, verbatim.")
    quantity: float | None = Field(None, description="Quantity as a number.")
    unit_price: float | None = Field(None, description="Price per unit, number only, no currency symbol.")
    total: float | None = Field(None, description="Line total as printed. Do not compute it yourself.")


class Invoice(BaseModel):
    invoice_number: str | None = Field(None, description="Invoice or document number, verbatim.")
    issue_date: str | None = Field(None, description="Issue date in ISO 8601 (YYYY-MM-DD).")
    due_date: str | None = Field(None, description="Payment due date in ISO 8601 (YYYY-MM-DD).")
    vendor_name: str | None = Field(None, description="Name of the party issuing the invoice.")
    vendor_tax_id: str | None = Field(None, description="Vendor VAT / tax / company registration number.")
    customer_name: str | None = Field(None, description="Name of the party being billed.")
    currency: str | None = Field(None, description="ISO 4217 code, e.g. USD, EUR, VND.")
    subtotal: float | None = Field(None, description="Subtotal before tax, as printed.")
    tax_amount: float | None = Field(None, description="Total tax amount, as printed.")
    total_amount: float | None = Field(None, description="Grand total payable, as printed.")
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="One entry per line on the invoice, in the order printed.",
    )


class Receipt(BaseModel):
    merchant_name: str | None = Field(None, description="Store or merchant name.")
    purchase_date: str | None = Field(None, description="Purchase date in ISO 8601 (YYYY-MM-DD).")
    purchase_time: str | None = Field(None, description="Purchase time in 24h HH:MM.")
    currency: str | None = Field(None, description="ISO 4217 code, e.g. USD, EUR, VND.")
    subtotal: float | None = Field(None, description="Subtotal before tax, as printed.")
    tax_amount: float | None = Field(None, description="Tax amount, as printed.")
    tip_amount: float | None = Field(None, description="Tip or service charge, as printed.")
    total_amount: float | None = Field(None, description="Total paid, as printed.")
    payment_method: str | None = Field(None, description="e.g. cash, visa, mastercard, qr.")
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="One entry per purchased item, in the order printed.",
    )


class PlainDocument(BaseModel):
    """Transcription baseline. Useful for measuring raw OCR quality and as a
    fallback when a document does not match any structured type."""

    title: str | None = Field(None, description="Document title or heading, if any.")
    language: str | None = Field(None, description="Dominant language as an ISO 639-1 code.")
    markdown: str | None = Field(
        None,
        description=(
            "Full readable content of the document as GitHub-flavoured markdown. "
            "Preserve reading order, headings and tables. Do not summarise."
        ),
    )


class DocumentType(BaseModel):
    name: str
    model: type[BaseModel]
    hint: str = ""

    model_config = {"arbitrary_types_allowed": True}


REGISTRY: dict[str, DocumentType] = {
    dt.name: dt
    for dt in [
        DocumentType(
            name="invoice",
            model=Invoice,
            hint="This is a commercial invoice or bill.",
        ),
        DocumentType(
            name="receipt",
            model=Receipt,
            hint="This is a point-of-sale receipt, often a narrow thermal-printed slip.",
        ),
        DocumentType(
            name="plain",
            model=PlainDocument,
            hint="Transcribe the document faithfully.",
        ),
    ]
}


def get(name: str) -> DocumentType:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown schema {name!r}; known: {sorted(REGISTRY)}") from None


def _harden(node: Any) -> Any:
    """Make a JSON schema acceptable to strict guided decoding.

    Every object gets additionalProperties: false, and every property is
    marked required. The fields are already nullable, so "required" here means
    "always emit this key" -- which is what we want: a missing key and a null
    are different failure modes, and only one of them is easy to score.
    """
    if isinstance(node, dict):
        node = {k: _harden(v) for k, v in node.items()}
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        return node
    if isinstance(node, list):
        return [_harden(v) for v in node]
    return node


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return _harden(model.model_json_schema())
