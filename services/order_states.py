"""Pure order/payment transition rules shared by the HTTP handlers and tests."""

ORDER_TRANSITIONS = {
    "placed": {"confirmed", "cancelled"},
    "confirmed": {"preparing", "cancelled"},
    "preparing": {"out_for_delivery", "cancelled"},
    "out_for_delivery": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}


def can_transition_order(current, target):
    if not isinstance(current, str) or not isinstance(target, str):
        return False
    return target in ORDER_TRANSITIONS.get(current, set())


def can_confirm_cash_payment(method, payment_status, order_status):
    return method == "cash_on_delivery" and payment_status == "pending" and order_status == "delivered"


def can_manage_payment(role, user_id, customer_id):
    """Admins manage any payment; customers only their own."""
    if role == "admin":
        return True
    return role == "customer" and bool(user_id) and str(user_id) == str(customer_id)
