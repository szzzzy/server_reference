证书工具包使用说明
==============
位置:本项目 certs\ 目录(纯 Python 实现,依赖 .venv_5090_llm 里的 cryptography 库)

一、已生成什么
  ca/ca.key  ca/ca.crt           根 CA(RSA 4096,10 年,自签) — 全系统的信任锚
  server/server.key server.crt   服务器证书(ECDSA P-256,2 年,SAN=localhost/127.0.0.1/本机 IP)
  clients/board-01.key board-01.crt   板卡客户端证书(3 年,用于 mTLS 双向认证)

二、常用命令(在项目根目录运行)
  .\.venv_5090_llm\Scripts\python.exe certs\generate_certs.py init            重新生成 CA
  ... generate_certs.py server --domain voice.xxx.com --ips 192.168.1.100     按客户域名/IP 重签服务器证书
  ... generate_certs.py client --board-id board-02                            给新板卡发客户端证书
  ... generate_certs.py list                                                  查看已有哪些证书
  ... generate_certs.py verify                                                校验链条与有效期

三、各方需要哪些文件
  服务器(5090/客户服务器):
    - server/server.key + server/server.crt   (HTTPS/WSS/MQTT-TLS 服务端)
    - ca/ca.crt                               (验证板卡客户端证书,开 mTLS 时)
  板卡(ESP32,烧录进固件):
    - ca/ca.crt                               (验证服务器身份,必须)
    - clients/<board-id>.key + .crt           (mTLS 时;含私钥,严禁外传明文)
  验收阶段客户自有域名证书:
    直接用客户提供的证书替换 server/server.crt/.key(如 Let's Encrypt),CA 与板卡证书不受影响。

四、安全须知
  1. 所有 *.key 为私钥,严禁提交到 git / 放入传输包 / 发给无关方;git 已通过 .gitignore 排除 certs\。
  2. 私钥泄露 = 立即吊销并重新签发(用 CA 重新签新证书,旧板卡换新)。
  3. CA 私钥(ca/ca.key)是最高机密:只有“签发证书”时才需要,日常服务端/板卡不需要。
  4. 证书有效期:CA 10 年、服务器 2 年、板卡 3 年;到期前 1 个月应续签(需重新签发,verify 可查日期)。
  5. 局域网开发阶段可不启用 TLS(明文);启用后请务必用服务器证书的 SAN(域名/IP)访问,否则板卡主机名校验失败。

五、与固件对接
  板卡侧 TLS 使用 mbedTLS:将 ca.crt 编译进固件作为根信任,hostname 校验开启。
  如果固件已有成熟的 MQTT/HTTPS/WSS 栈,优先复用其 TLS 配置,仅把“证书文件”替换为本工具包产物。
