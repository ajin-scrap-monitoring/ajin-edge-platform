import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from ajin_edge.transport import DeliveryClient, RetryBackoff, validate_https_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/data",
        "https://u:p@host/data",
        "https://host/data?token=secret",
        "https://host/data#fragment",
    ],
)
def test_unsafe_delivery_url_is_rejected(url):
    with pytest.raises(ValueError):
        validate_https_url(url)


@pytest.mark.parametrize(
    "status,body,kind",
    [
        (200, {"accepted": True, "measurement_id": "m1"}, "accepted"),
        (200, {"accepted": True, "measurement_id": "wrong"}, "retry"),
        (409, {"duplicate": True, "measurement_id": "m1"}, "accepted"),
        (409, {}, "quarantine"),
        (401, {}, "fatal"),
        (403, {}, "fatal"),
        (422, {}, "quarantine"),
        (429, {}, "retry"),
        (503, {}, "retry"),
    ],
)
async def test_http_outcomes_never_ack_unconfirmed_or_retry_poison(status, body, kind):
    # Transport is the only double; HTTP payload/classification code remains real.
    def backend(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Idempotency-Key"] == "m1"
        assert json.loads(request.content) == {"measurement_id": "m1"}
        return httpx.Response(status, json=body, headers={"Retry-After": "99999"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as http:
        client = DeliveryClient("https://backend.example/measurements", "test-token", client=http)
        result = await client.send({"measurement_id": "m1"})
        assert result.kind == kind
        assert result.retry_after <= 60


def test_backoff_is_bounded_and_requires_stable_success_before_reset():
    backoff = RetryBackoff(jitter=lambda: 0)
    assert [backoff.failure() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    backoff.success(now=100)
    backoff.success(now=129)
    assert backoff.failure() == 60
    backoff.success(now=200)
    backoff.success(now=231)
    assert backoff.failure() == 1


async def test_real_tls_roundtrip_and_untrusted_certificate_rejected(tmp_path):
    # Runtime TLS behavior, using a local self-signed test-only certificate.
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"measurement_id":"tls-1","accepted":true}')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"https://localhost:{server.server_port}/measurements"
    try:
        async with httpx.AsyncClient(trust_env=False) as untrusted:
            result = await DeliveryClient(url, "token", client=untrusted).send(
                {"measurement_id": "tls-1"}
            )
            assert result.kind == "retry"
            assert received == []
        context = ssl.create_default_context(cafile=str(cert_path))
        async with httpx.AsyncClient(verify=context, trust_env=False) as trusted:
            result = await DeliveryClient(url, "token", client=trusted).send(
                {"measurement_id": "tls-1"}
            )
            assert result.kind == "accepted"
        assert received == [{"measurement_id": "tls-1"}]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


async def test_http_request_has_total_deadline_not_only_per_read_timeout():
    import asyncio

    async def stalled_backend(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"measurement_id": "m1", "accepted": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(stalled_backend)) as http:
        client = DeliveryClient(
            "https://backend.example/data", "token", client=http, total_timeout=0.02
        )
        result = await client.send({"measurement_id": "m1"})
        assert result.kind == "retry"
        assert result.reason == "REQUEST_TIMEOUT"
