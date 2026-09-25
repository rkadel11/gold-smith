#!/usr/bin/env python3
"""HTTPS server for the Mac Mini, reachable from any device on the same wifi.

Serves a local directory (default: this repo's data/ folder, so the iPhone
widget or another Mac can read current.json straight off the Mac Mini
instead of waiting on GitHub) over TLS on the LAN.

First run creates a small private certificate authority plus a server
certificate signed by it, in ~/.gold-smith-ssl/. Only the CA file
(gold-smith-local-ca.crt) needs to be installed on other devices, once --
after that, the server certificate can be regenerated freely (new IP,
renamed Mac) without touching the phones again.

Usage (on the Mac Mini, from the repo folder):
    python3 scripts/mac_mini_https_server.py
    python3 scripts/mac_mini_https_server.py --port 8443 --dir data
    python3 scripts/mac_mini_https_server.py --regen    # new server cert

Stdlib only; uses macOS's built-in /usr/bin/openssl (LibreSSL) for keys.
"""

import argparse
import functools
import http.server
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SSL_DIR = Path.home() / ".gold-smith-ssl"
CA_KEY = SSL_DIR / "gold-smith-local-ca.key"
CA_CRT = SSL_DIR / "gold-smith-local-ca.crt"
SERVER_KEY = SSL_DIR / "server.key"
SERVER_CRT = SSL_DIR / "server.crt"

# iOS/macOS reject TLS server certs valid for more than 825 days.
SERVER_DAYS = 825
CA_DAYS = 3650


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def lan_ip():
    """The IP this Mac uses on the wifi (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def bonjour_name():
    """The Mac's `<name>.local` hostname, e.g. raj-mac-mini.local."""
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"],
                             capture_output=True, text=True, check=True)
        name = out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        name = socket.gethostname().split(".")[0]
    return f"{name}.local"


def ensure_ca():
    if CA_KEY.exists() and CA_CRT.exists():
        return
    print(f"Creating local certificate authority in {SSL_DIR}")
    run("openssl", "genrsa", "-out", str(CA_KEY), "2048")
    os.chmod(CA_KEY, 0o600)
    ext = SSL_DIR / "ca.ext"
    ext.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\nprompt=no\n"
        "[dn]\nCN=Gold Smith Local CA\n"
        "[v3_ca]\nbasicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
    )
    run("openssl", "req", "-x509", "-new", "-key", str(CA_KEY), "-sha256",
        "-days", str(CA_DAYS), "-config", str(ext), "-out", str(CA_CRT))


def ensure_server_cert(hosts, regen=False):
    if SERVER_KEY.exists() and SERVER_CRT.exists() and not regen:
        return
    print("Creating server certificate for: " + ", ".join(hosts))
    san = []
    for h in hosts:
        try:
            ipaddress.ip_address(h)
            san.append(f"IP:{h}")
        except ValueError:
            san.append(f"DNS:{h}")
    csr = SSL_DIR / "server.csr"
    ext = SSL_DIR / "server.ext"
    ext.write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        f"subjectAltName={','.join(san)}\n"
    )
    run("openssl", "genrsa", "-out", str(SERVER_KEY), "2048")
    os.chmod(SERVER_KEY, 0o600)
    run("openssl", "req", "-new", "-key", str(SERVER_KEY),
        "-subj", f"/CN={hosts[0]}", "-out", str(csr))
    run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(CA_CRT),
        "-CAkey", str(CA_KEY), "-CAcreateserial", "-sha256",
        "-days", str(SERVER_DAYS), "-extfile", str(ext),
        "-out", str(SERVER_CRT))
    csr.unlink()


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Always fresh data; let the Scriptable widget / browsers fetch it.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--dir", default=str(REPO_ROOT / "data"),
                   help="folder to serve (default: repo data/)")
    p.add_argument("--host", action="append", default=[],
                   help="extra hostname/IP to put in the cert (repeatable)")
    p.add_argument("--regen", action="store_true",
                   help="regenerate the server cert (e.g. after IP change)")
    args = p.parse_args()

    SSL_DIR.mkdir(mode=0o700, exist_ok=True)
    ip = lan_ip()
    name = bonjour_name()
    hosts = [name, "localhost", "127.0.0.1"] + ([ip] if ip else []) + args.host

    ensure_ca()
    ensure_server_cert(hosts, regen=args.regen)

    serve_dir = Path(args.dir).resolve()
    if not serve_dir.is_dir():
        sys.exit(f"Not a folder: {serve_dir}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(SERVER_CRT, SERVER_KEY)

    handler = functools.partial(Handler, directory=str(serve_dir))
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(f"\nServing {serve_dir} over HTTPS on port {args.port}")
    print(f"  https://{name}:{args.port}/")
    if ip:
        print(f"  https://{ip}:{args.port}/")
    print(f"\nCA to install on other devices (once): {CA_CRT}")
    print("Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
