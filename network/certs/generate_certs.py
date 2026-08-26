#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
证书生成工具(网络服务器方案,本地/验收通用)
================================================
生成一套 PKI:
  ca/ca.key, ca/ca.crt                     根 CA(10 年,自签,RSA 4096)
  server/server.key, server/server.crt     服务器证书(2 年,ECDSA P-256,含 SAN)
  clients/<board_id>.key/.crt              板卡客户端证书(3 年,ECDSA P-256,mTLS 用)

用法(使用 .venv_5090_llm 的 python):
  python generate_certs.py                     # 一把梭:CA + server(本机 IP) + board-01 客户端
  python generate_certs.py server --domain voice.example.com --ips 192.168.1.100
  python generate_certs.py client --board-id board-02
  python generate_certs.py list
  python generate_certs.py verify

注意:
  * 私钥文件(.*.key)严禁进 git/压缩包外传;签名用私钥 = 该证书作废。
  * 验收阶段若客户域名证书由客户提供(Let's Encrypt 等),直接用客户证书替换
    server/server.crt 即可,CA 与板卡证书不变。
  * ESP32 板卡侧需要:ca.crt(根证书,验证服务器)+ 自己的 client.crt/client.key
    (若开 mTLS);服务器侧需要 ca.crt(验证板卡)。
"""
import argparse
import datetime
import ipaddress
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HERE = Path(__file__).resolve().parent
CA_DIR = HERE / "ca"
SERVER_DIR = HERE / "server"
CLIENTS_DIR = HERE / "clients"

CA_DAYS = 3650          # 根 CA 10 年
SERVER_DAYS = 730       # 服务器证书 2 年
CLIENT_DAYS = 1095      # 板卡证书 3 年


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _pem_priv(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _pem_pub(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _load_priv(path, password=None):
    return serialization.load_pem_private_key(path.read_bytes(), password=password)


def _load_cert(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def _now_cert(subject, issuer, pub, priv, days, ca=False, san_dns=None, san_ips=None,
              client_auth=False, server_auth=False):
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
    )
    if san_dns or san_ips:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(d) for d in san_dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in san_ips]
            ),
            critical=False,
        )
    ekus = []
    if server_auth:
        ekus.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if client_auth:
        ekus.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    if ekus:
        builder = builder.add_extension(x509.ExtendedKeyUsage(ekus), critical=False)
    return builder.sign(priv, hashes.SHA256())


def _write(name, data):
    path = HERE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"  生成 {path.relative_to(HERE)}")


def local_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if ":" not in addr:
                ips.append(addr)
    except OSError:
        pass
    ips.append("127.0.0.1")
    ips = sorted(set(ips))
    return ips


def cmd_init(args):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Voice Robot Local CA")])
    ca_cert = _now_cert(name, name, ca_key.public_key(), ca_key, CA_DAYS, ca=True)
    _write("ca/ca.key", _pem_priv(ca_key))
    _write("ca/ca.crt", _pem_pub(ca_cert))
    print("  [init] CA 完成")


def cmd_server(args):
    (HERE / "ca").mkdir(parents=True, exist_ok=True)
    if not (HERE / "ca/ca.key").exists():
        print("  [server] 先执行 init 生成 CA"); sys.exit(1)
    ca_key = _load_priv(HERE / "ca/ca.key")
    ca_cert = _load_cert(HERE / "ca/ca.crt")
    skey = ec.generate_private_key(ec.SECP256R1())
    dns = ["localhost"]
    if args.domain:
        dns.append(args.domain)
    ips = local_ips() if not args.ips else args.ips.split(",")
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.domain or "voice-server-local")])
    cert = _now_cert(name, ca_cert.subject, skey.public_key(), ca_key, SERVER_DAYS,
                     san_dns=dns, san_ips=[i.strip() for i in ips], server_auth=True)
    _write("server/server.key", _pem_priv(skey))
    _write("server/server.crt", _pem_pub(cert))
    print(f"  [server] SAN dns={dns} ips={ips}")


def cmd_client(args):
    (HERE / "ca").mkdir(parents=True, exist_ok=True)
    if not (HERE / "ca/ca.key").exists():
        print("  [client] 先执行 init 生成 CA"); sys.exit(1)
    ca_key = _load_priv(HERE / "ca/ca.key")
    ca_cert = _load_cert(HERE / "ca/ca.crt")
    ckey = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.board_id)])
    cert = _now_cert(name, ca_cert.subject, ckey.public_key(), ca_key, CLIENT_DAYS, client_auth=True)
    _write(f"clients/{args.board_id}.key", _pem_priv(ckey))
    _write(f"clients/{args.board_id}.crt", _pem_pub(cert))


def cmd_list(args):
    for d in (CA_DIR, SERVER_DIR, CLIENTS_DIR):
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.suffix in (".key", ".crt"):
                    print(("  PRIVATE" if f.suffix == ".key" else "  public "), f.relative_to(HERE))
        else:
            print(f"  (缺 {d.relative_to(HERE)})")


def cmd_verify(args):
    from cryptography.exceptions import InvalidSignature
    ca = _load_cert(HERE / "ca/ca.crt")
    print(f"  CA  : {ca.subject.rfc4514_string()}  有效至 {ca.not_valid_after_utc:%Y-%m-%d}")
    for p in sorted(SERVER_DIR.glob("*.crt")):
        c = _load_cert(p)
        try:
            _check_issued(c, ca)
            print(f"  服务器 {p.name}: OK  有效至 {c.not_valid_after_utc:%Y-%m-%d}  SAN={c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value}")
        except InvalidSignature:
            print(f"  服务器 {p.name}: 签名校验失败!")
    for p in sorted(CLIENTS_DIR.glob("*.crt")):
        c = _load_cert(p)
        try:
            _check_issued(c, ca)
            print(f"  板卡  {p.name}: OK  有效至 {c.not_valid_after_utc:%Y-%m-%d}")
        except InvalidSignature:
            print(f"  板卡  {p.name}: 签名校验失败!")


def _check_issued(cert, issuer):
    cert.verify_directly_issued_by(issuer)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sp = sub.add_parser("server")
    sp.add_argument("--domain", default="")
    sp.add_argument("--ips", default="", help="逗号分隔,默认自动取本机所有 IPv4 + 127.0.0.1")
    cp = sub.add_parser("client")
    cp.add_argument("--board-id", default="board-01")
    sub.add_parser("list")
    sub.add_parser("verify")
    args = ap.parse_args()
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
