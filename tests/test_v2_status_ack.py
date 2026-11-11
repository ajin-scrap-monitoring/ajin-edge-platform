from ajin_edge.v2.transport import DeliveryClient, Result


async def test_status_ack_proves_which_warning_state_was_accepted(monkeypatch):
    client = DeliveryClient("https://backend.example", "test", None)
    result = Result("accepted", received_at="2026-11-11T00:00:01Z")

    async def respond(payload):
        return result

    monkeypatch.setattr(client, "_send", respond)
    body = {
        "message_type": "edge_status",
        "message_id": "status-one",
        "generated_at": "2026-11-11T00:00:00Z",
        "overall_state": "DEGRADED",
        "reason_codes": ["OUTBOX_DATA_LOSS"],
    }
    await client.send(body)
    body["reason_codes"].clear()
    proof = client.metrics["edge_status"]["latest_accepted_status"]
    assert proof["reason_codes"] == ["OUTBOX_DATA_LOSS"]
    assert proof["message_id"] == "status-one"
    result = Result("retry", "NETWORK_OR_TIMEOUT")
    body.update(message_id="status-two", overall_state="HEALTHY")
    await client.send(body)
    assert client.metrics["edge_status"]["latest_accepted_status"] == proof
    result = Result("accepted", received_at="2026-11-11T00:00:02Z")
    await client.send(body)
    proof = client.metrics["edge_status"]["latest_accepted_status"]
    assert proof["message_id"] == "status-two"
    assert proof["reason_codes"] == []
    assert proof["overall_state"] == "HEALTHY"
