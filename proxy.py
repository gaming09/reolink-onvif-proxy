import asyncio
import ipaddress
import json
import logging
import os
import re


LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "18890"))
CAMERA_NAME = os.getenv("CAMERA_NAME", "reolink-camera")
TARGET_HOST = os.getenv("CAMERA_HOST", os.getenv("TARGET_HOST", ""))
TARGET_PORT = int(os.getenv("CAMERA_PORT", os.getenv("TARGET_PORT", "8000")))
TIMEOUT = float(os.getenv("IO_TIMEOUT", "15"))
ALLOWED_CLIENTS = tuple(
    ipaddress.ip_network(value.strip(), strict=False)
    for value in os.getenv("ALLOWED_CLIENTS", "").split(",")
    if value.strip()
)

ONVIF_NAMESPACES = {
    b"tt": b"http://www.onvif.org/ver10/schema",
    b"tds": b"http://www.onvif.org/ver10/device/wsdl",
    b"trt": b"http://www.onvif.org/ver10/media/wsdl",
    b"tptz": b"http://www.onvif.org/ver20/ptz/wsdl",
    b"timg": b"http://www.onvif.org/ver20/imaging/wsdl",
    b"tev": b"http://www.onvif.org/ver10/events/wsdl",
    b"tmd": b"http://www.onvif.org/ver10/deviceIO/wsdl",
    b"ter": b"http://www.onvif.org/ver10/error",
}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")


async def read_request(reader: asyncio.StreamReader) -> bytes:
    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
    content_length = 0
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
            break
    body = await asyncio.wait_for(reader.readexactly(content_length), TIMEOUT) if content_length else b""
    lines = [line for line in header[:-4].split(b"\r\n") if not line.lower().startswith(b"connection:")]
    return b"\r\n".join(lines) + b"\r\nConnection: close\r\n\r\n" + body


def request_host(request: bytes) -> bytes:
    """Return the proxy address supplied by the ONVIF client."""
    for line in request.split(b"\r\n"):
        if line.lower().startswith(b"host:"):
            value = line.split(b":", 1)[1].strip()
            if value and not any(char in value for char in (b"\r", b"\n", b"/", b"\\")):
                return value
    return b""


def normalize_raw_xml(body: bytes, advertised_host: bytes) -> bytes:
    """Repair framing and keep discovered ONVIF services on the proxy."""
    envelope = re.search(rb"<(?:SOAP-ENV|soap-env|s):Envelope\b", body)
    if envelope:
        tag_end = body.find(b">", envelope.start())
        if tag_end != -1:
            additions = []
            opening_tag = body[envelope.start() : tag_end]
            for prefix, namespace in ONVIF_NAMESPACES.items():
                if prefix + b":" in body and b"xmlns:" + prefix + b"=" not in opening_tag:
                    additions.append(b' xmlns:' + prefix + b'="' + namespace + b'"')
            if additions:
                body = body[:tag_end] + b"".join(additions) + body[tag_end:]
    # Some Reolink firmware emits prefixed xsi:type values that Zeep tries to
    # use as literal XML tag names. The WSDL already defines these types, so
    # removing the redundant hints preserves the data and restores parsing.
    body = re.sub(rb"\s+xsi:type=(['\"]).*?\1", b"", body)
    if advertised_host:
        target = f"{TARGET_HOST}:{TARGET_PORT}".encode()
        body = body.replace(b"http://" + target, b"http://" + advertised_host)
        body = body.replace(b"https://" + target, b"http://" + advertised_host)
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/soap+xml; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )


def soap_envelope_complete(body: bytes) -> bool:
    """Return whether a malformed raw response contains a full SOAP envelope."""
    return re.search(rb"</(?:SOAP-ENV|soap-env|s):Envelope\s*>", body) is not None


def client_allowed(peer: object) -> bool:
    if not ALLOWED_CLIENTS:
        return True
    if not isinstance(peer, tuple) or not peer:
        return False
    address = ipaddress.ip_address(peer[0])
    return address.is_loopback or any(address in network for network in ALLOWED_CLIENTS)


async def send_health(writer: asyncio.StreamWriter) -> None:
    body = json.dumps(
        {"status": "ok", "camera": CAMERA_NAME, "target": f"{TARGET_HOST}:{TARGET_PORT}"},
        separators=(",", ":"),
    ).encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    await writer.drain()


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        request = await read_request(client_reader)
        request_line = request.split(b"\r\n", 1)[0]
        if request_line.startswith(b"GET /health "):
            await send_health(client_writer)
            return
        if not client_allowed(peer):
            logging.warning("Rejected connection from %s", peer)
            client_writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await client_writer.drain()
            return
        if not TARGET_HOST:
            raise ValueError("CAMERA_HOST is required")

        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(TARGET_HOST, TARGET_PORT), TIMEOUT
        )
        upstream_writer.write(request)
        await upstream_writer.drain()

        first = await asyncio.wait_for(upstream_reader.read(65536), TIMEOUT)
        if first.startswith((b"<?xml", b"<SOAP-ENV", b"<s:Envelope")):
            logging.warning("Normalized malformed ONVIF response from %s for %s", TARGET_HOST, peer)
            body = first
            # Affected Reolink firmware sends a complete XML document without
            # HTTP framing and may leave the TCP connection open. Stop reading
            # as soon as the SOAP envelope closes instead of waiting for EOF.
            while not soap_envelope_complete(body):
                chunk = await asyncio.wait_for(upstream_reader.read(65536), TIMEOUT)
                if not chunk:
                    break
                body += chunk
            client_writer.write(normalize_raw_xml(body, request_host(request)))
            await client_writer.drain()
        else:
            client_writer.write(first)
            await client_writer.drain()
            while True:
                chunk = await asyncio.wait_for(upstream_reader.read(65536), TIMEOUT)
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()

        upstream_writer.close()
        await upstream_writer.wait_closed()
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError, ValueError) as exc:
        logging.error("Proxy request from %s failed: %s", peer, exc)
    finally:
        client_writer.close()
        await client_writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    logging.info("Listening on %s:%s and forwarding to %s:%s", LISTEN_HOST, LISTEN_PORT, TARGET_HOST, TARGET_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
