import pb
from mesht_device import PORTNUMS, BROADCAST_NUM


class ParsedTextPacket:
    def __init__(self, text, sender_num, sender_hex, to_num, channel, is_direct):
        self.text = text
        self.sender_num = sender_num
        self.sender_hex = sender_hex
        self.to_num = to_num
        self.channel = channel
        self.is_direct = is_direct


class ParsedDeliveryPacket:
    def __init__(self, request_id, status, error_reason, reporter_hex):
        self.request_id = request_id
        self.status = status
        self.error_reason = error_reason
        self.reporter_hex = reporter_hex


def parse_text_packet(packet):
    # Parse a MeshPacket text payload into a structured result.
    if not isinstance(packet, dict):
        return None
    decoded = packet.get("decoded") or {}
    if PORTNUMS.get(decoded.get("portnum")) != "TEXT_MESSAGE_APP":
        return None

    payload = decoded.get("payload") or b""
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8", errors="replace")
    elif isinstance(payload, str):
        text = payload
    else:
        text = ""

    sender_num = packet.get("from")
    sender_hex = None
    if sender_num is not None:
        sender_hex = f"{int(sender_num) & 0xFFFFFFFF:08x}"

    to_num = packet.get("to")
    is_direct = to_num is not None and int(to_num) != BROADCAST_NUM

    return ParsedTextPacket(
        text=text,
        sender_num=sender_num,
        sender_hex=sender_hex,
        to_num=to_num,
        channel=int(packet.get("channel") or 0),
        is_direct=is_direct,
    )


def parse_delivery_packet(packet):
    # Parse routing delivery status into a structured result.
    if not isinstance(packet, dict):
        return None
    decoded = packet.get("decoded") or {}
    if PORTNUMS.get(decoded.get("portnum")) != "ROUTING_APP":
        return None

    request_id = decoded.get("request_id")
    if request_id is None:
        return None
    if int(request_id) <= 0:
        return None

    payload = decoded.get("payload")
    if not isinstance(payload, (bytes, bytearray)):
        return None
    try:
        routing = pb.decode(bytes(payload), [("int32", "error_reason", 3)]) or {}
    except Exception:
        return None

    error_reason = routing.get("error_reason")
    # Only the error_reason routing variant maps to delivery status.
    # Route request/reply routing packets do not carry ACK/FAIL results.
    if error_reason is None:
        return None

    reporter = packet.get("from")
    reporter_hex = None
    if reporter is not None:
        reporter_hex = f"{int(reporter) & 0xFFFFFFFF:08x}"
    return ParsedDeliveryPacket(
        request_id=int(request_id),
        status=("ack" if int(error_reason) == 0 else "failed"),
        error_reason=int(error_reason),
        reporter_hex=reporter_hex,
    )


def direct_peer_hex(packet, my_node_id):
    # Return the other side of a direct message from a MeshPacket.
    if not isinstance(packet, dict):
        return None
    to_num = packet.get("to")
    from_num = packet.get("from")
    if to_num is None or int(to_num) == BROADCAST_NUM:
        return None
    if from_num is None:
        return None

    my_hex = my_node_id or ""
    if my_hex == "00000000":
        return f"{from_num & 0xFFFFFFFF:08x}"
    try:
        my_num = int(my_hex, 16)
    except Exception:
        return None

    if from_num == my_num:
        peer = to_num
    elif to_num == my_num:
        peer = from_num
    else:
        return None
    return f"{peer & 0xFFFFFFFF:08x}"


def is_dm_for(text_info, node_id):
    # True when this parsed text packet is a direct message addressed to node_id.
    if text_info is None:
        return False
    if not text_info.is_direct:
        return False

    to_num = text_info.to_num
    sender_num = text_info.sender_num
    if to_num is None or sender_num is None:
        return False

    try:
        my_num = int(node_id or "", 16)
    except Exception:
        return False

    return int(to_num) == my_num and int(sender_num) != my_num
