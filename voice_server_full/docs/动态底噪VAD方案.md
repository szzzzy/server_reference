# 动态底噪 VAD 改造方案（规划稿，未执行）

> 状态：**仅规划，不执行**。本文档落地后，先提交当前版本基线，再按本方案分阶段实施。
> 范围：`voice_server_full/`（服务器侧 `engine/board_serial_asr_test.py` + `server/real_engine.py`）。
> 设备端（ESP32 WakeNet / 硬件唤醒阈值）、WSS 协议、固件**均不动**。

---

## 1. 背景与目标

### 1.1 问题定义

当前 VAD 的底噪是"一次校准、全程不变"：

- 每次 WSS 会话开始、收到首帧后，`_calibrate_background()` 用前 2.5s 真实音频的帧电平 **10% 分位**估一个 `background_dbfs`（`server/real_engine.py:436`）；
- 3 次校准失败回退配置默认 **-60 dBFS**（`config.json → voice.real.vad.background_dbfs`）；
- 之后整个会话（含多轮 MIC_START 循环）的起始/端点阈值恒为 `background_dbfs + 3dB`（`engine/board_serial_asr_test.py:227-230`）。

环境不变时无问题；环境变化时依次出现四类故障：

1. 噪声升过 `背景+3dB` → 噪声被当成"语音起始"（误触发）；
2. 噪声持续高于端点阈值 → 活跃帧持续抵扣尾静音计数 → **端点永不触发**；
3. 只能靠 `max_seconds=15s` 硬截断 → 说完话后白等最多 15s，且噪声被送进 ASR（乱文/幻觉轮）；
4. 噪声下降或设备挪动后阈值相对过高 → 轻声、远场话漏检。

### 1.2 目标

把"固定底噪"升级为"**随环境变化的底噪估计**"，使阈值始终保持"当前底噪 + 固定信噪比偏移"，消除上述四类故障，且：

- 默认关闭、可配置、可回归（`dynamic_floor: false` 时行为与现状完全一致）；
- 纯服务器侧改动，不动协议/固件/设备端；
- 估计器必须抗"语音污染"，即语音帧不得抬高底噪。

---

## 2. 设计

### 2.1 核心思路

`capture_until_endpoint()` 内把 `background_dbfs` 从**常量**改成**状态变量 `bg_t`**（每帧循环内更新）；
起始阈值 = `bg_t + start_above_db`、端点阈值 = `bg_t + end_above_db`，每帧重新计算（只是两次加法，零额外成本）。
其余判定逻辑（120ms/300ms 起始窗、400ms 尾静音、4 倍抵扣）**一行不动**。

### 2.2 底噪估计器（三个机制缺一不可）

```
状态:
  bg_t          —— 当前底噪估计(初值 = 现有 _calibrate_background 结果或配置默认)
  ring          —— 环形缓冲,保存最近 8s(400 帧)的候选帧 dBFS
  last_update   —— 上次重算分位时刻

每 20ms 帧(在 capture_until_endpoint 帧循环内):
  candidate = (真实帧) 且 (frame_dbfs < bg_t + gate_db) 且 (frame_dbfs > -100dB)
  ① 门控:   只有 candidate 才入 ring —— 低于当前阈值的帧才可能是噪声;
             高于阈值的一律不碰,语音(高于阈值)永远进不了估计器
  ② 滑动分位:每 0.5s 对 ring 重算一次 10% 分位 p(与现有校准同构,抗突发/语音污染)
  ③ 限速+钳制:step = clamp(p - bg_t,
                          -down_max_db_per_05s, +up_max_db_per_05s)
             bg_t = clamp(bg_t + step, floor_min_dbfs, floor_max_dbfs)

合成静音帧排除: 合成帧为 -120dB,已被 "> -100dB" 条件排除;
             另外复用 real_bytes_total 判定"真实帧",双保险(与现有校准一致)。
```

参数语义（默认值见 §3）：

- **上升快、下降慢**：`up_max_db_per_s=3`（尽快跟上突变噪声，缩短噪声误触发期）；`down_max_db_per_s=0.5`（下降信号可能来自语音间隙，慢一点；分位本身已抗污染，限速只是保险）；
- `gate_db=12`：候选门控余量（比阈值偏移 3dB 宽，让"噪声略高于旧阈值"时仍能被吸收——这是能让底噪**跟上噪声上升**的关键：旧阈值以下的噪声帧永远存在，直到新噪声完全覆盖）；
- `floor_min_dbfs=-80 / floor_max_dbfs=-35`：语义钳制，避免病态值。

### 2.3 数据可得性约束（设计前提，务必理解）

- **回合内**：VAD 收集期间有连续真实帧 → 动态估计可用；起始判定前的帧 + 说话间隙低于阈值的帧都是有效素材；
- **回合间**：TTS 播报期间设备停传（MIC_STOP），且 RoundBuffer 补的是 -120dB 合成帧 → **没有真实噪声信息**；
- 因此跨回合环境变化只能靠：① 本回合开始（MIC_START 后用户开口前）的帧；② 每轮语音前的帧。两者都被估计器自动吸收，**无需额外机制**；
- 长安静期（播报 40s+ 后环境变了）到下一轮开口之间学习窗口较短 → 由 `up_max_db_per_s=3` 保证几秒内基本跟上；若仍不足，用 §6 Phase 0 的"回合级重估"兜底。

### 2.4 与现有校准的关系

`_calibrate_background()` **保留**：作为 `bg_t` 的初值来源（3 次失败回退配置默认）。动态模式把"一次性校准"变成"持续学习"，初值精度要求大幅降低——校准失败不再致命。

---

## 3. 配置设计（`server/config.json`）

在 `voice.real.vad` 下新增段（动态模式默认**关**，保持可回归）：

```json
"vad": {
  ...现有字段不变...,
  "dynamic_floor": {
    "enabled": false,
    "window_s": 8.0,
    "percentile": 10,
    "up_max_db_per_s": 3.0,
    "down_max_db_per_s": 0.5,
    "gate_db": 12.0,
    "floor_min_dbfs": -80.0,
    "floor_max_dbfs": -35.0
  }
}
```

- `enabled=false` 时：`bg_t` 恒等于初值（现状行为，代码路径唯一化但结果等价）；
- 旧配置（无 `dynamic_floor` 段）按 `enabled=false` 处理，**旧配置零迁移成本**。

---

## 4. 代码变更清单

| # | 文件 | 函数/位置 | 改动 | 风险 |
|---|---|---|---|---|
| 1 | `engine/board_serial_asr_test.py` | `capture_until_endpoint()` 签名+帧循环 | 新增可选参数 `dynamic_floor=None`（dict）；内部 `background_dbfs` 变 `bg_t` 状态；帧循环内做门控入 ring + 每 0.5s 分位重算 + 限速钳制；阈值改每帧取 `bg_t + offset` | 低（缺省时行为等价，需回归测试） |
| 2 | 同上 | 诊断字典 | 新增 `bg_trajectory`（每 0.5s 采样一次，供日志/状态页/CSV）、`bg_final_dbfs`、`dynamic_floor_enabled` | 无 |
| 3 | `server/real_engine.py` | `_load_and_answer_loop()` VAD 参数解析段（~L296） | 解析 `dynamic_floor` 段并传入 `capture_until_endpoint` | 低 |
| 4 | `server/config.json` | `voice.real.vad` | 增加 `dynamic_floor` 段（默认关） | 无 |
| 5 | `docs/算法链路说明.md` | §2/§10 参数表 | 补动态底噪说明与参数表 | 文档 |

可选（Phase 2，独立小改动）：`end_above_db` 与 `start_above_db` 解耦，端点用更保守偏移（4~6dB）——目前两者同为 3dB，说话中误截断风险偏高。此项**不属于**动态底噪必须项。

---

## 5. 不变的部分

- 起始判定（120ms/300ms 窗）、端点判定（尾静音 400ms + 4 倍抵扣）、`max_seconds=15s` 兜底逻辑；
- `_calibrate_background()`（只改初值语义）、RingBuffer 补静音机制、`real_bytes_total` 过滤；
- 协议（MIC_STOP/SPKS/PCM/SPKE/MIC_START）、设备端一切（WakeNet、硬件唤醒阈值 `MICS` 参数、固件）；
- 本地串口版 V5 链路（`voice_sleep_v5_5090` 等）：因其调用 `capture_until_endpoint` 时**不传** `dynamic_floor`，行为不变。

---

## 6. 分阶段实施计划

| 阶段 | 内容 | 交付物 | 验证方式 | 预估 |
|---|---|---|---|---|
| **P0 回合级重估（可选、低成本）** | 每轮 MIC_START 后、用户开口前用最近 1.5s 真实帧重估 `bg`（复用现有 percentile 逻辑，只改调用时机）；与主方案可共存 | 开关 `reroll_per_turn` | 真机多轮 + 播报期间开风扇 | 0.5 天 |
| **P1 帧级动态估计（主方案）** | §4 变更 #1~#4 | 开关默认关 + 诊断字段 | 回归：无开关时与基线逐帧一致 | 1~2 天 |
| **P2 阈值解耦** | `end_above_db` 独立调高（4~6dB），`start_above_db` 可选调低至 2dB | config 默认值调整 | A/B | 0.5 天 |
| **P3 A/B 验证** | 录制"安静 → 开风扇（+10dB）→ 说话 → 关风扇"连续音频，固定/动态双跑 | 对比报告 | 指标见 §7 | 1 天 |
| **P4 默认开启 + 文档** | `enabled` 置 true、参数定稿、`算法链路说明.md` 更新 | 发布版 | 真机联调 | 0.5 天 |

**P3 判定指标**（与现状基线对比）：

- 误起始率（安静期噪声触发轮数 / 总时长分钟数）；
- 端点延迟（说完 → 端点）在噪声场景下是否保持 ~400ms（现状会拉向 15s）；
- 截断质量：speech_start_seconds / last_active_seconds 与人工标注差；
- 漏检率：噪声+轻声场景下空轮占比；
- `bg_trajectory` 曲线与实测底噪（人工测量）偏差 ≤ 2dB。

---

## 7. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 语音污染：语音帧抬高底噪 → 尾字截断/端点提前 | **高** | 门控（仅低于阈值帧入 ring）+ 10% 分位（语音占比 <50% 时天然安全）+ 上升限速 3dB/s（语音即使漏进门控，推动力也受限） |
| 环境突变（风扇突开 +10dB）后几秒盲区 | 中 | 上升限速放宽至 3dB/s；P0 回合级重估兜底；`max_seconds` 硬截断仍是最低层防线 |
| 合成静音帧（-120dB）拉低底噪 | 中 | 已由 `> -100dB` + `real_bytes_total` 双重排除（现有校准同款逻辑） |
| 多轮/长会话漂移累积 | 低 | 每轮起始前帧自动学习；P0 兜底 |
| 回归风险（开关关闭时行为漂移） | 低 | P1 交付前做"开关关 vs 现状"逐帧一致性回归；P4 才默认开 |

---

## 8. 明确不做的事

- 不改设备本地唤醒逻辑（`set_hardware_sleep`/`MICS` 硬件阈值）——那是另一套机制，与服务器 VAD 解耦，本次不联动；
- 不做噪声抑制/降噪（NS）、不做模型类 VAD（WeNet/Silero 等）——本方案是能量域改造，若后续要 AEC/降噪再单独立项；
- 不动 `network/`（旧快照目录）——只改 `voice_server_full/`。
