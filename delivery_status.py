ROUTING_ERROR_TEXT = {
    1: "Failed to deliver to mesh",
    2: "Failed to deliver to mesh",
    3: "Failed to deliver to mesh",
    4: "No radio interface",
    5: "Failed to deliver to mesh",
    6: "Channel/key mismatch",
    7: "Message is too large to send",
    8: "No app response",
    9: "Duty cycle limit",
    32: "Invalid request",
    33: "Not authorized",
    34: "Could not send encrypted message",
    35: "Recipient needs your key",
    36: "Admin session expired",
    37: "Admin key not authorized",
    38: "Rate limited",
    39: "Recipient key unavailable",
}


def routing_error_text(error_reason):
    try:
        return ROUTING_ERROR_TEXT.get(int(error_reason), "Failed to deliver to mesh")
    except Exception:
        return "Failed to deliver to mesh"


def delivery_status_text(status, ack_nodes=None, direct_peer_hex=None, error_reason=None):
    normalized = (status or "waiting").lower()
    if normalized == "waiting":
        return "Sending..."
    if normalized == "failed":
        return routing_error_text(error_reason)
    if normalized == "ack":
        if direct_peer_hex:
            peer = direct_peer_hex.lower()
            nodes = {(node or "").lower() for node in (ack_nodes or set())}
            if peer in nodes:
                return "Delivered to recipient"
            return "Relayed, not confirmed by recipient"
        return "Delivered to mesh"
    return "Sending..."
