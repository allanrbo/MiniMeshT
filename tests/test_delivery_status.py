from delivery_status import delivery_status_text, routing_error_text


def test_delivery_status_text_for_channel_ack():
    assert delivery_status_text("ack", ack_nodes={"00000002"}) == "Delivered to mesh"


def test_delivery_status_text_for_direct_ack_from_recipient():
    assert (
        delivery_status_text("ack", ack_nodes={"00000002"}, direct_peer_hex="00000002")
        == "Delivered to recipient"
    )


def test_delivery_status_text_for_direct_ack_from_relay():
    assert (
        delivery_status_text("ack", ack_nodes={"00000003"}, direct_peer_hex="00000002")
        == "Relayed, not confirmed by recipient"
    )


def test_routing_error_text_uses_canonical_channel_key_mismatch():
    assert routing_error_text(6) == "Channel/key mismatch"
