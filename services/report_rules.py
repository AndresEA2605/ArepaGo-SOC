"""Validation and workflow rules for customer and storefront reports."""
import uuid

REPORT_CATEGORIES = {"web_issue", "store_complaint"}
REPORT_STATUSES = {"new", "in_review", "resolved", "closed"}
REPORT_TRANSITIONS = {
    "new": {"in_review", "resolved", "closed"},
    "in_review": {"new", "resolved", "closed"},
    "resolved": {"in_review", "closed"},
    "closed": {"in_review"},
}


def validate_report(data):
    category = data.get("category")
    if not isinstance(category, str) or category not in REPORT_CATEGORIES:
        raise ValueError("category debe ser web_issue o store_complaint")
    subject = data.get("subject")
    message = data.get("message")
    if not isinstance(subject, str) or not 5 <= len(subject.strip()) <= 160:
        raise ValueError("subject debe tener entre 5 y 160 caracteres")
    if not isinstance(message, str) or not 15 <= len(message.strip()) <= 4000:
        raise ValueError("message debe tener entre 15 y 4000 caracteres")
    order_id = data.get("order_id")
    if order_id in (None, ""):
        order_id = None
    else:
        try:
            order_id = str(uuid.UUID(str(order_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("order_id no es un identificador válido") from exc
    return {"category": category, "subject": subject.strip(), "message": message.strip(), "order_id": order_id}


def can_transition_report(current, target):
    return isinstance(current, str) and isinstance(target, str) and target in REPORT_TRANSITIONS.get(current, set())
