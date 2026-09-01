#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
固件内嵌 CA 归属校验工具(仅标准库,可在任意编译机/交付机运行)
================================================================
用途:确认某份固件(elf/bin 里的 ca_cert.pem 或 build 目录里的 ca_cert.pem.S)
内嵌的根 CA 到底是哪一代,以便与服务器证书配对。

  python verify_fw_ca.py --build <ESP-IDF build目录>   # 从 build/ca_cert.pem.S 反查(推荐,权威)
  python verify_fw_ca.py --bin   <xxx.elf|xxx.bin>    # 从二进制里搜嵌入的 PEM(elf 有效;压缩的 app.bin 可能搜不到)

输出:"新CA(2026-08-28)"/"旧CA(2026-08-24)"/"未知" + 指纹。
"""
import argparse
import base64
import hashlib
import re
import sys
from pathlib import Path

# 已知 CA 指纹(证书 DER 的 SHA-256,与 openssl x509 -fingerprint 一致)
KNOWN = {
    "f00307e420bd09816ee220882db9cc9aa587a451d9be7e7d1ea4517cacefd42a": "新CA(2026-08-28 重签,当前全系统信任锚)",
    "21fc47f86533cdce0207ff95d6c2a73b168cf87277bc5976e78b08492b1c1e67": "旧CA(2026-08-24,已作废,仅留档)",
    "a62f9b38b6d26fd6537f9c9f3d9ac12d2c2802afd0a51951e79afadba6e61932": "ESP 示例默认 CA(CN=ESP,非本系统)",
}


def pem_fingerprints(pem: bytes):
    """PEM -> DER -> sha256(证书指纹);返回固件中可能存在的全部证书块。"""
    out = []
    for m in re.finditer(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S):
        body = m.group(0)
        try:
            b64 = b"".join(body.splitlines()[1:-1])
            der = base64.b64decode(b64)
        except Exception:
            continue
        if der:
            out.append((hashlib.sha256(der).hexdigest(), len(der)))
    return out


def from_build_dir(build: Path):
    s = build / "ca_cert.pem.S"
    if not s.exists():
        return None, f"缺少 {s} (未找到 build 产物)"
    data = b""
    for line in s.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith(".byte"):
            for x in re.findall(r"0x([0-9a-fA-F]{2})", line):
                data += bytes([int(x, 16)])
    data = data.split(b"\x00", 1)[0]
    return pem_fingerprint(data), None


def from_bin(path: Path):
    data = path.read_bytes()
    return pem_fingerprint(data), None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", help="ESP-IDF build 目录(取 build/ca_cert.pem.S)")
    ap.add_argument("--bin", help="固件文件(.elf 可搜到;xz 压缩的 app.bin 可能搜不到)")
    args = ap.parse_args()
    if not args.build and not args.bin:
        print("用法: python verify_fw_ca.py --build <build目录> 或 --bin <固件>")
        sys.exit(2)

    fp, err = None, None
    src = ""
    if args.build:
        fp, err = from_build_dir(Path(args.build))
        src = f"{args.build}\\ca_cert.pem.S"
    else:
        fp, err = from_bin(Path(args.bin))
        src = args.bin

    if err:
        print(f"[FAIL] {err}")
        sys.exit(1)
    if not fp:
        print(f"[FAIL] {src}: 未找到内嵌 CA PEM(若是压缩 app.bin,请改用 --build 反查)")
        sys.exit(1)

    fp_hex, der_len = fp
    label = KNOWN.get(fp_hex, "未知 CA(不在已知清单,请人工核对)")
    print(f"来源  : {src}")
    print(f"内嵌CA: {label}")
    print(f"指纹  : {fp_hex}")
    print(f"DER   : {der_len} bytes")
    if fp_hex.startswith("f00307e4"):
        print("结果  : OK —— 与新 CA(2026-08-28)一致,可与当前服务器证书配对")
    elif fp_hex.startswith("21fc47f8"):
        print("结果  : WARN —— 仍是旧 CA(2026-08-24),将无法验证当前服务器证书!")
    sys.exit(0)


if __name__ == "__main__":
    main()
