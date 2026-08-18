"""
Fetch the CockroachDB Cloud CA chain over the Postgres TLS handshake.

Windows has no ~/.postgresql/root.crt and CockroachDB Cloud's CA is not in the
OS trust store, so sslmode=verify-full fails out of the box. Rather than
downgrading TLS verification, we pull the chain the server actually presents
and pin it.

Writes: certs/root.crt
"""

import os
import socket
import ssl
import struct
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

url = urlparse(os.environ["DATABASE_URL"])
host, port = url.hostname, url.port or 26257

# Postgres SSLRequest: int32 length=8, int32 code=80877103
sock = socket.create_connection((host, port), timeout=15)
sock.sendall(struct.pack("!ii", 8, 80877103))
resp = sock.recv(1)
if resp != b"S":
    sys.exit(f"server refused TLS, replied {resp!r}")

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
tls = ctx.wrap_socket(sock, server_hostname=host)

chain = tls.get_unverified_chain()
if not chain:
    sys.exit("server presented no certificate chain")

os.makedirs("certs", exist_ok=True)
pems = [ssl.DER_cert_to_PEM_cert(c) for c in chain]
with open("certs/root.crt", "w") as f:
    f.write("".join(pems))

print(f"host      : {host}")
print(f"chain len : {len(chain)} cert(s)")
for i, der in enumerate(chain):
    # decode just enough to show subject/issuer
    import cryptography.x509 as x509  # noqa: PLC0415

    c = x509.load_der_x509_certificate(der)
    same = c.subject == c.issuer
    print(f"  [{i}] subject={c.subject.rfc4514_string()[:70]}")
    print(f"      issuer ={c.issuer.rfc4514_string()[:70]}{'   <-- self-signed root' if same else ''}")
print("\nwrote certs/root.crt")
tls.close()
