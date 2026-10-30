"""Pure rules for the incident lifecycle, kept free of database imports."""

ACTIONS = {"awaiting_traffic", "recovering", "resolved", "resolved_inactive"}


def recovery_decision(requests, recovery_count, recovery_required, seconds_since_breach,
                      inactivity_seconds):
    """Decide what to do with an open incident after a non-degraded measurement.

    A measurement without traffic is not evidence of recovery, so it neither
    increments nor resets the consecutive counter. It only closes the incident
    once the configured inactivity window has elapsed, because a service that
    receives no requests cannot be validated by traffic at all.

    Returns ``(action, recovery_count, resolution_reason)``.
    """
    if recovery_required < 2:
        raise ValueError("recovery_required debe ser al menos 2")
    if seconds_since_breach < 0:
        raise ValueError("seconds_since_breach no puede ser negativo")
    if requests <= 0:
        if inactivity_seconds > 0 and seconds_since_breach >= inactivity_seconds:
            return "resolved_inactive", recovery_count, "inactivity_timeout"
        return "awaiting_traffic", recovery_count, None
    count = recovery_count + 1
    if count >= recovery_required:
        return "resolved", count, "recovered_with_traffic"
    return "recovering", count, None
