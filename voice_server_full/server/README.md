# 虚拟测试服务器(独立构建,不动现有代码)

按固件通信协议基线实现的服务器虚拟测试端 —— **不与现有语音流水线代码耦合**:
现有 `voice_daemon.py` / `board_serial_asr_test.py` / `realtime_pipeline.py` 等零改动;
本目录自带配置、venv、测试产物与启动脚本。

## 一、目录

```
network/server/
  run_server.py            主入口(broker + HTTPS 文件 + WSS 语音 + MQTT 控制面)
  board_simulator.py       板卡模拟器(OTA/audio/voice/filedump 自动化验收)
  make_test_artifacts.py   生成测试产物(固件 bin + wav + manifest.json)
  config.json              服务器配置(端口/主题/token/证书路径)
  session_hub.py           会话中心(设备注册/事件去重/状态)
  manifest_store.py        发布清单与 OTA/audio 应答规则(含隔离)
  mqtt_adapter.py          MQTT 控制面(OTA/audio 检查应答、状态、vcmd)
  https_file_server.py     HTTPS 文件服务(Range/ETag/SHA-256 + /__status)
  wss_adapter.py           WSS 语音服务(Bearer/PCM1/命令/FILE_SEND)
  voice_bridge.py          语音桥(stub 统计引擎;real 引擎预留接口)
  broker.py                本地 MQTT broker(amqtt,1883/8883 TLS)
  releases/manifest.json   发布清单(由 make_test_artifacts 生成)
  releases/files/          固件 + 音频文件
  runs/                    每次运行的日志与事件
```

## 二、安装(一次性,无需联网)

```
python -m venv .venv_vs
# websockets 从现有 .venv 复制(纯 Python 包,已自带)
robocopy .venv\Lib\site-packages\websockets .venv_vs\Lib\site-packages\websockets /E
robocopy .venv\Lib\site-packages\websockets-17.0.1.dist-info .venv_vs\Lib\site-packages\websockets-17.0.1.dist-info /E
```

> 依赖说明:`websockets`(WSS 用)从项目 `.venv` 复制;**MQTT 为纯标准库实现**(`mqtt_pure.py`,
> 无需 amqtt/paho);证书生成用 `certs/` 自带脚本(Python cryptography,已在 .venv_5090_llm)。
> 若环境可联网,也可以 `pip install websockets`(版本 ≥12 兼容)。

证书:使用项目根 `certs/` 已生成的自签 CA + 服务器证书(含本机 IP SAN;换网络后需重签或改域名,见 certs/README)。

## 三、启动(自动生成产物,若缺)

```
network\server\start_virtual_server.cmd
```

或手动:

```
.venv_vs\Scripts\python.exe network\server\make_test_artifacts.py     # 生成固件/音频/manifest
.venv_vs\Scripts\python.exe network\server\run_server.py              # 启动全部服务
```

端点:

| 服务 | 地址 |
|---|---|
| MQTT broker | 127.0.0.1:1883(可开 8883 TLS) |
| HTTPS 文件 | https://<本机IP>:8443(固件 /fw/app.bin,音频 /audio/greeting.wav) |
| WSS 语音 | wss://<本机IP>:9443/voice(Bearer 令牌,见 config.json) |
| 状态查询 | GET /__status(文件服务上,JSON:设备/引擎/清单) |

注意:`make_test_artifacts.py --addr` 生成的 URL 主机名**必须与固件 `CONFIG_JULIA_SERVER_ADDR` 一致**(OTA URL 白名单精确匹配);换机器/换网络后重新生成 manifest,或改 config.json 的 `server.addr`。

## 四、自动化验收(板卡模拟器)

另开终端:

```
.venv_vs\Scripts\python.exe network\server\board_simulator.py --scenario all
```

场景:

| 场景 | 验证内容 |
|---|---|
| `ota` | check→response 校验(回显/字段/SHA)→ status 序列 → 下载(200/Content-Length/Range 206/ETag/If-Range回退/SHA-256)→ 同版本二次检查 update=false |
| `audio` | check→response 校验 → status 序列 → 下载校验(≤4 MiB) |
| `voice` | WSS Bearer 连接 → MIC_START → PCM1×N(20ms)→ MIC_STOP → 服务器状态断言(帧数/seq 连续/命令记录) |
| `filedump` | 服务器先下发 vcmd `FILE_SEND SD:/test.wav`(如 `run_server.py --admin-vcmd "FILE_SEND SD:/test.wav" --admin-vcmd-delay 6`)→ 模拟器 BEGIN/二进制/END → FILE_OK → 落盘确认 |

退出码:0=全部 PASS,1=有 FAIL。

## 五、与真机固件联调(M2,后续)

1. 固件 `CONFIG_JULIA_SERVER_ADDR` 设为虚拟服务器地址(与本机 IP / 域名一致);
2. MQTT TLS:固件用系统证书捆绑;本地自签 CA 需固件团队把 `certs/ca/ca.crt` 并入信任,或测试期关 MQTT TLS(本服务器 1883 默认明文);
3. WSS token:config.json `wss.token` 与固件 `WSS_TOKEN` 一致;
4. `voice.mode: stub → real`:real 引擎由独立模块 `voice_bridge_real.py` 实现(直接 import 现有 `board_serial_asr_test`/`asr_eval_core`/`realtime_pipeline`,不在现有文件上插入);
5. 全部按《固件协议基线文档》校验规则实现;差异/疑点见方案文档"与固件团队待确认项"。

## 六、实现原则

- **独立构建**:本目录自包含;现有代码只被 import 复用(未来 real 引擎),不做任何修改;
- **透传语义**:PCM1 帧格式、命令文本与固件协议一一对应;推理层接入只发生在 voice_bridge 的 real 引擎;
- **可插拔**:broker 可外接(`--no-broker`+config mqtt.host);TLS 可用 certs/ 或客户证书。

## 七、真实语音引擎(一问一答,R1–R3 已完成并实测)

**工作流**:WSS PCM1 → RingBuffer(字节流兼容层)→ 现有 VAD/ASR → Qwen3(无提示词、无历史、一问一答)→ 分句 TTS 子进程 → SPKS+PCM+SPKE 下行。设计书:`语音工作流设计.md`。

### 启动(必须用 GPU 环境 python)

```
.venv_5090_llm\Scripts\python.exe network\server\run_server.py --voice-mode real
```

或把 `config.json → voice.mode` 改为 `"real"` 后再启动。

### 验收(模拟器播放人声 → 断言下行)

```
.venv_5090_llm\Scripts\python.exe network\server\board_simulator.py --scenario voice --expect-downlink
```

### 实测数据(2026-08-24,samples/标准女声)

| 项 | 数值 |
|---|---|
| 模型加载(ASR→TTS→Qwen) | ~20–30 s |
| ASR 识别(3 s 语音) | ~0.5 s |
| 首句下行(SPKS→PCM→SPKE) | 语音结束后 ~10 s |
| 一轮完整回答(180 tokens,11 段) | ~46 s |
| 下行编码 | SPKS <rate> + PCM ≤1200B/帧 + SPKE |

### 关键机制

- **字节流兼容层 RingBuffer**:实现 serial 同形接口(read/reset_input_buffer/flush),并**在语音停流后自动补合成静音帧(默认 1.4 s,按 endpoint_ms 配置)**,使现有 `capture_until_endpoint` 的尾部静音端点逻辑照常生效(这是 WSS"推送帧"与串口"持续流"适配的关键);
- **一问一答**:无唤醒词/无对话状态机/无系统提示词(`voice.real.system_prompt` 默认空;`max_new_tokens` 默认 180,建议按需调小到 60–120 以快速回应);
- **分句**:重建版 `_sentence_chunks`(原版含面向控制台的 print,在 GBK 控制台遇 emoji 会崩溃 → 不能直接复用,按规则重建,逻辑一致);
- **并发**:WSS 异步收发;采集/ASR/Qwen/TTS 顺序在 GPU 上串行(与现状一致);下行经 `call_soon_threadsafe` 调度到事件循环广播。

### 已知边界

- 客户端断开后:下行帧被丢弃(合成继续,属正常);
- 每问独立(history_turns=0);回答风格如需简短,调 `max_new_tokens` 或加 system_prompt;
- 板卡上传模式(MICS 触发 / MICW 持续)—— 服务器两种都兼容,首条命令差异只是配置。

### 与新版 PROTOCOL.md(设备侧实际实现)的一致性

- **权威协议参考**:项目根 `PROTOCOL.md`(julia-fused-base 实际实现);
- **本地唤醒词**:设备带本地唤醒词(如"你好小智")→ 设备自触发开麦(MIC_START 等效),**服务器无需主动发 MIC_START**,引擎即收即答;
- **vcmd(MQTT)命令集**:`FILE_SEND/MIC_START/MIC_STOP/MICW/MICS/SPKV`——**不含 SPKS/SPKE/SPKT**(那些只走 WSS 下行);服务器默认只在 WSS 发语音命令,无冲突;
- **固件镜像头**:`make_test_artifacts.py` 生成的 app.bin 带 `project_name=julia-ai`(PROTOCOL.md §3.5 要求);
- 协议自测断言全过(OTA 7/7、Audio 16/16、Voice 7/7 实测)。
