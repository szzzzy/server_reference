# 动态底噪 VAD 改造方案（执行稿）

> 状态：**执行稿，P1~P3 已实施**（双窗估计器 + 会话级接入 + 诊断 + 引擎级 heal + snap 快速回落 + A/B 工具 `tools/vad_ab.py`，默认关闭可回归，见 §9/§12）；P4（默认开启+定稿）前待真机全链路 A/B。
> 范围：`voice_server_full/` 中服务器侧 VAD。
> 本稿整合：双时间尺度估计器、会话级状态、快速窗无门控修正、滞回方向修正、协议边界（MIC_START/MIC_STOP）确认。

---

## 1. 问题

当前 VAD 的 `background_dbfs` 在会话开始时通过 `_calibrate_background()` 估计一次，之后整个会话保持不变。

当前阈值：

```text
start_threshold = background_dbfs + start_above_db
end_threshold   = background_dbfs + end_above_db
```

环境噪声变化后会出现：

* 底噪升高：噪声误触发 speech start；
* 噪声持续高于 endpoint threshold：尾静音无法累计，端点迟迟不触发；
* 最终依赖 `max_seconds=15s` 强制截断；
* 底噪下降后：阈值偏高，轻声或远场语音可能漏检。

目标是把：

```text
固定 background_dbfs
```

改为：

```text
动态 bg_t
```

使 VAD 阈值持续跟随当前环境。

---

## 2. 核心设计

`capture_until_endpoint()` 内：

```text
background_dbfs
```

只作为初始值，之后维护：

```text
bg_t
```

每帧重新计算：

```text
start_threshold = bg_t + start_above_db
end_threshold   = bg_t + end_above_db
```

现有 speech start、endpoint、尾静音、`max_seconds` 等状态机逻辑不改。

**关键约束（A 修正）：`bg_t` 与两个 ring 必须是会话级状态**。`capture_until_endpoint()` 每轮返回后即结束，若 `bg_t` 是函数局部变量，每轮都会从校准初值重新开始，跨回合学习全部失效。因此：

```text
engine/board_serial_asr_test.py 新增 NoiseFloorTracker 类
    ├─ 持有 bg_t + fast/slow ring + 更新状态
    ├─ 接口: on_frame(...) / bg() / reset(bg) / diagnostics()
    └─ 与 VAD 状态机解耦,本地串口 V5 链路亦可复用

RealVoiceEngine(会话级) 持有一个实例
    └─ 每轮调用 capture_until_endpoint(..., floor_tracker=self._floor)

floor_tracker=None 时行为与现状完全一致(回归保障)
```

---

## 3. 动态底噪估计

单一 8 s 窗口不适合同时处理噪声上升和下降。

原因是：

* 噪声下降时，低电平新样本会很快拉低 P10；
* 噪声上升时，旧低噪声样本会长期占据 P10，导致跟随过慢。

因此使用两个时间尺度。

## 3.1 Fast window

负责环境变吵：

```text
最近约 1.5 s(仅真实帧,见 §4 修正 B)
→ 10% percentile(不对样本套 gate,见 §4)
→ p_fast
```

当：

```text
p_fast > bg_t + rise_trigger_db
```

持续若干次后：

```text
bg_t
```

以较快速度向 `p_fast` 上升。

默认：

```text
up_max_db_per_s = 3.0
```

## 3.2 Slow window

负责稳定估计和环境变安静：

```text
最近约 8 s candidate frames(套 gate,见 §4)
→ 10% percentile
→ p_slow
```

如果：

```text
p_slow < bg_t
```

则 `bg_t` 缓慢下降。

默认：

```text
down_max_db_per_s = 0.5
```

**静音快速回落（snap，P3 新增）**：0.5 dB/s 在"噪声刚停 1~2 s 即说轻声"时追不上（数十秒），
因此当分位显著低于当前估计（`p_slow < bg_t - snap_trigger_db`，默认 6 dB）时直接重锚
`bg_t = p_slow`。适用前提：仅预语音段运行（语音期冻结）、以 8 s 分位为锚（不被语音间隙
误导）——等价于把"分位差距过大"视为一次安静期重校准。

最终效果：

```text
环境变吵 → 快速跟随
环境变静 → 缓慢回落;落差大 → 直接重锚
```

---

## 4. Candidate 过滤与更新时机（修正 B：gate 悖论）

**Fast 窗口不得对样本套 gate。**

原因：噪声突变（风扇/空调从安静环境启动）常比底噪高 **15~20 dB**（如 -55 → -35），
超过 `gate_db=12`。若 fast 窗口也套 gate：

* 噪声帧全部被排除 → 1.5 s 后 ring 中旧安静样本过期 → `fast_min_frames` 不满足；
* `p_fast` 无法计算 → **底噪永不上升** → "噪声突升 → 端点不触发 → 15 s 超时"这一目标故障完全没被修复。

因此 fast 窗口的候选定义：

```text
fast_candidate = real_frame  AND frame_dbfs > -100
```

只排除合成静音帧（RingBuffer 补的 -120 dB 帧），不对电平值设上限。

**污染的防护改由"更新时机"保证**：

```text
允许估计器更新(上升/下降) ⟺ 本轮尚未判定 speech_started(即每轮预语音段)
speech_started 置位后 → 本轮冻结估计器
```

效果：

* 预语音段内，即使噪声 +20 dB，`p_fast ≈ 新噪声值` → 上升确认后按 3 dB/s 跟上；
* 语音一旦开始，**语音结构性进不了估计器**（不只是统计上难进）；
* 每轮预语音段都会重新学习 → 跨轮自适应成立。

**Slow 窗口保留 gate**（保守稳定）：

```text
slow_candidate = real_frame
                 AND frame_dbfs > -100
                 AND frame_dbfs < bg_t + gate_db
```

轻声或低 SNR 语音仍可能 slow 窗口进入，因此多道防线共同降低污染风险：

```text
slow: gate + percentile + 最小样本数 + 0.5 dB/s 下降限速
fast: 预语音段时机限制 + 上升确认 + 3 dB/s 上升限速
```

---

## 5. Ring 实现

ring 中保存：

```text
(timestamp, frame_dbfs)
```

而不是简单使用：

```python
deque(maxlen=N)
```

每次更新前删除超出时间窗的数据。

例如：

```text
fast_ring：仅保留最近 1.5 s
slow_ring：仅保留最近 8 s
```

这样窗口语义始终是真正的"最近 N 秒"，不会因为 candidate 较少而残留很久以前的数据。

---

## 6. 更新逻辑

默认每：

```text
0.5 s
```

更新一次（帧循环内按时间戳检查，无需独立线程）。

伪代码：

```python
if not speech_started:                       # 修正 B:仅预语音段
    if enough_fast_samples and p_fast > bg_t + rise_trigger_db:
        if rise_condition_confirmed:         # 连续 2 次满足
            bg_t += min(
                p_fast - bg_t,
                up_max_db_per_s * update_interval_s
            )
    elif enough_slow_samples and p_slow < bg_t:
        bg_t -= min(
            bg_t - p_slow,
            down_max_db_per_s * update_interval_s
        )
```

最后：

```python
bg_t = clamp(
    bg_t,
    floor_min_dbfs,
    floor_max_dbfs
)
```

默认：

```text
floor_min_dbfs = -80
floor_max_dbfs = -35
```

注：`rise_trigger_db=1.0` + 连续 2 次确认 ≈ 实际延迟 0.5~1 s 才开始上升，可接受。
上升分支优先于下降（`elif`），环境过渡期内以升起为准，避免"未跟上先回落"。

---

## 7. 配置

在 `voice.real.vad` 下新增：

```json
"dynamic_floor": {
  "enabled": false,

  "fast_window_s": 1.5,
  "slow_window_s": 8.0,
  "percentile": 10,

  "update_interval_s": 0.5,

  "up_max_db_per_s": 3.0,
  "down_max_db_per_s": 0.5,

  "gate_db": 12.0,

  "rise_trigger_db": 1.0,
  "rise_confirm_updates": 2,
  "snap_trigger_db": 6.0,

  "fast_min_frames": 20,
  "slow_min_frames": 50,

  "floor_min_dbfs": -80.0,
  "floor_max_dbfs": -35.0
}
```

默认：

```text
enabled = false
```

旧配置中不存在 `dynamic_floor` 时同样按关闭处理。

---

## 8. 与现有校准、会话状态及协议边界的关系

### 8.1 现有校准

现有：

```python
_calibrate_background()
```

保留。

它只负责给：

```text
bg_t
```

提供初始值（`NoiseFloorTracker.reset(bg)`）。

之后动态 estimator 根据真实音频持续调整。

因此原来的：

```text
一次校准 → 全程固定
```

变成：

```text
一次校准 → 获得初值 → 每轮预语音段持续修正
```

### 8.2 协议边界（补充确认：语音开始 MIC_START，结束 MIC_STOP，本次不改）

* **语音开始**：`MIC_START`（或设备本地唤醒后的等效自触发开麦）→ 设备进入 LISTENING 并上传 PCM1 帧；
* **语音结束**：VAD 判"说完了" → 服务器发 `MIC_STOP` → 设备停止上传、UI 进 THINKING；
* **真实帧只存在于 [MIC_START, MIC_STOP] 窗口内**；窗口之外（TTS 播报、空闲）仅有 RingBuffer 合成的 -120 dB 静音帧，无任何环境信息；
* 因此动态底噪的学习素材 = **每轮窗口内的预语音段（用户开口前的帧）+ 说话间隙低电平帧**——正好就是估计器唯一允许更新的时段（§4）；
* 本方案**不改变** `MIC_START`/`MIC_STOP` 的发送时机与语义，协议零改动。

### 8.3 数据可得性约束（设计前提）

* **回合内**：VAD 收集期间有连续真实帧 → 预语音段 + 说话间隙有素材；
* **回合间**：见 8.2——无真实噪声信息；
* 因此跨回合环境变化全靠"下一轮预语音段"学习——这正是 `bg_t` 会话级持有的原因；
* 长安静期后环境已变、下一轮开口前学习窗口较短 → 由 `up_max_db_per_s=3` + 上升确认保证几秒内基本跟上；
* 每轮失败兜底不变：`max_seconds=15s` 硬截断仍是最终防线。

---

## 9. 代码改动

| 文件 | 改动 |
| --- | --- |
| `engine/board_serial_asr_test.py` | 新增 `NoiseFloorTracker` 类（bg_t/fast/slow ring/on_frame/bg/reset/diagnostics） |
| 同上 | `capture_until_endpoint()` 新增 `floor_tracker=None` 参数；帧循环内调 `tracker.on_frame()`；start/end threshold 改每帧按 `tracker.bg()`（为 None 时用 `background_dbfs`，行为与现状等价） |
| 同上 | 诊断字典新增 `bg_initial_dbfs`、`bg_final_dbfs`、`dynamic_floor_enabled`、`bg_trajectory`（由 tracker 提供） |
| `server/real_engine.py` | `_load_and_answer_loop()`：解析 `dynamic_floor` 段；会话级创建/持有 tracker；校准完成后 `reset(bg)`；每轮传入 `capture_until_endpoint` |
| `server/config.json` | `voice.real.vad` 新增 `dynamic_floor` 段（默认关） |
| `docs/算法链路说明.md` | §2/§10 补动态底噪说明与参数表 |

不动的部分：

* 起始判定（120 ms/300 ms 窗）、端点判定（尾静音 + 4 倍抵扣）、`max_seconds` 兜底；
* `_calibrate_background()`、RingBuffer 补静音机制、`real_bytes_total` 过滤；
* 协议（MIC_START/MIC_STOP/SPKS/PCM/SPKE 时序与语义）、设备端一切（WakeNet、硬件唤醒阈值 `MICS`、固件）；
* 本地串口 V5 链路：不传 `floor_tracker` → 行为不变。

---

## 10. 诊断信息

至少记录：

```text
bg_initial_dbfs
bg_final_dbfs
dynamic_floor_enabled
bg_trajectory
```

`bg_trajectory` 每 0.5 s 记录：

```text
time
bg_t
p_fast
p_slow
fast_candidate_count
slow_candidate_count
update_reason
speech_state            ← 调参时一眼看出污染时刻
```

没必要把所有 VAD 内部变量都塞进日志。

---

## 11. 阈值解耦（滞回方向修正）

动态 floor 第一阶段不修改：

```text
start_above_db
end_above_db
```

先只验证底噪动态跟踪是否有效。

后续如果需要做 VAD hysteresis，再单独测试：

```text
start_above_db > end_above_db
```

例如：

```text
start = +4~6 dB
end   = +2~3 dB
```

**方向说明（修正初稿错误）**：初稿曾建议调高 `end_above_db`（4~6 dB），方向反了——endpoint
threshold 越高，越容易把轻声尾音/清辅音/换气判成静音，导致提前截断。正确滞回方向是"进入门
槛高、保持门槛低"：

* `start` 高：抗噪声误起始；
* `end` 低：容忍轻声尾音，防提前截断；
* 噪声抗性不再依赖偏移量，而由动态底噪承担——这正是动态底噪稳定后（P4）再解耦的前提。

> **2026-08-28 归档**：本项已随"线上唤醒·分层灵敏度"落地——config 现状即
> `voice.real.vad.start_above_db=5.0 / end_above_db=3.0`（start(+5) > end(+3)，滞回方向
> 与上表一致，对话态生效），且已随真机联调验证（改前：噪声误起始 0.00s + 15s 无端点；
> 改后：恢复轮 2.16s 起始 / 4.92s 端点 / 400ms 尾静音）。本项完成。

---

## 12. 实施阶段

### P0 基线（已完成提交，待归档回归素材）

* 现有版本基线已提交（`c637380`）；
* 回归素材基本免费：`voice_server_full/server/runs/*/audio/qa_*.wav`（每轮输入音频已落盘）
  + `voice_qa_results.csv` + 日志中每轮 VAD 诊断（`endpoint_triggered / speech_start_seconds /
  trailing_silence_ms / vad_threshold_dbfs`）；
* 归档一份"固定 PCM 集 + 当前 VAD 输出"作为 regression baseline；
* 建议制作一张拼接 wav（安静 3 s → 风扇 5 s → 说话 → 关风扇），回放/回归/A-B/调参四用，
  经 `board_simulator.py --scenario voice`（`voice_wav` 路径可换）驱动。

### P1 动态 floor 框架

* 实现 §9 全部改动：`NoiseFloorTracker`、`dynamic_floor` 参数、诊断字段；
* 默认关闭；
* 验收：`dynamic_floor=false` 时与当前版本输出一致（逐帧/逐轮回归）；
* 预估：1~2 天。

### P2 开启动态 floor

* 使用固定 PCM A/B：

```text
fixed floor
vs
dynamic floor
```

重点测试：

```text
安静 → 开风扇 → 说话
```

以及：

```text
开风扇 → 关风扇 → 轻声说话
```

* **附加项（必做）引擎级 heal**：见 §15 风险表"噪声突升/恒噪"——P1 实现验证表明
  "下一轮预语音段恢复"在突变噪声下不成立（0.12s 内被 speech_started 冻结抢占），
  需在引擎判定"噪声型轮次"后用该轮音频重锚 `bg_t`；heal 仅在 `dynamic_floor` 开启时生效。
* **P2 已完成（本稿落库时）**：引擎级 heal 已实现（`real_engine.py`，噪声型轮次 = 判起始但无端点
  + 空识别或活跃占比≈100% → 帧电平 10% 分位重锚 + 空轮收尾）；A/B 工具 `tools/vad_ab.py`
  6/6 通过——恒噪场景本轮两者均误起始（帧级救不了本轮），heal 后第二轮动态底噪仅在
  人声处起始（2.18s）/固定底噪仍每轮都坏；回落场景确认 bg_t -36→-39 生效、轻声捕获仅滞后
  0.16s。关键结论见 §14。

### P3 参数调整（已完成，本稿落库时）

根据 A/B 调整：

```text
fast_window_s
gate_db
up_max_db_per_s
down_max_db_per_s
rise_trigger_db
```

* **P3 完成结论**：保留默认 `fast_window_s=1.5 / gate_db=12 / up_max_db_per_s=3 /
  down_max_db_per_s=0.5 / rise_trigger_db=1`；依据 S3 残缺点新增 **`snap_trigger_db=6`**
  （静音快速回落，见 §3.2）。`tools/vad_ab.py` 扩到 S4 场景，9/9 通过：
  - S4 实测：无 snap 时 bg_t -36→-37.5、起始 3.30s；有 snap 时 bg_t -36→**-58.2**（重锚到安静
    分位）、起始 **3.14s** —— 噪声刚停即轻声的场景由 snap 兜底；
  - S3 实测：6s 安静期 bg_t -36→-58.2 快速回落，轻声捕获与固定底噪同速（0.00s 差）；
  - P1 回归 14/14 通过（snap 未破坏上升/冻结/回落语义）。

> 状态：**执行稿，P1~P4 已完成**（双窗估计器 + 会话级接入 + 诊断 + 引擎级 heal + snap 快速回落 + 确定性 A/B + 真机全链路 A/B；`dynamic_floor.enabled=true` 默认开启；start/end 阈值解耦留待体验打磨）。

### P4 真机全链路 A/B（2026-08-28 完成）

* `enabled` 置 `true`（本稿状态行）；阈值解耦（`start > end` 滞回）留待体验打磨，未开启。
* **隔离三轮对比**（`tools/servertest_vad_ab.py`，服务器 9444 端口隔离真机，TLS SAN 与端口无关；
  引擎为会话级全局实例，真实 ESP32 仅连着 MQTT 不抢语音流；探测 0.017s/帧节奏 + 纯噪声轮
  turn1 喂校准）：

| 轮次(固定底噪) | 动态底噪+heal |
|---|---|
| turn2: 起始 0.00s / 无端点 / 15s 超时 | turn2: 同上 + **heal: bg_t -60.0→-36.3dB**（ASR 有字→保留作答） |
| turn3: 起始 0.00s / 无端点 / 15s 超时（每轮都坏） | turn3: **起始 2.16s(仅人声) / 端点 4.92s 正常 / 阈值 33.3dB** |

* **实施中发现并修复**：heal 原将"重锚"与"丢弃文本"绑定，实测量活跃占比 0.977 < 0.98 门槛导致
  漏修（0.98 目标成了"刚好够不着"）。已解耦为：无端点轮**一律重锚** bg_t（P10，钳制
  [-80,-35]），仅"空识别或活跃占比≈100%"的纯噪声轮才丢弃文本走空轮 —— 混合轮（噪声+可识别
  人声）重锚后仍正常作答，下一轮即恢复。

### P4 阈值解耦

只有动态 floor 稳定后，再按 §11 调整 start/end threshold。

---

## 13. 验证指标

重点只看四项：

### 误起始率

```text
无语音区间 false speech start 次数 / 分钟
```

### Endpoint delay

```text
VAD endpoint - 人工标注真实语音结束
```

### Timeout rate

```text
15 s timeout rounds / total rounds
```

### 漏检 / 截断

检查：

* 轻声是否漏掉；
* 开头是否丢失；
* 尾字是否提前截断。

辅助观察（P3 调参主指标）：

```text
bg_trajectory 是否合理跟随环境变化;
底噪恢复时间: bg_t 与人工/实测底噪差 ≤ 2 dB 所需秒数
```

---

## 14. 预期效果

最终逻辑：

```text
_calibrate_background()
        ↓
      bg 初值
        ↓
     dynamic bg_t
      ↙       ↘
fast window   slow window
噪声上升      噪声下降
      ↘       ↙
        bg_t
         ↓
动态 start/end threshold
         ↓
现有 VAD 状态机
```

核心原则：

> **不重写 VAD，只把固定底噪参考值变成动态底噪参考值。**

> P2 实测确认（详见 §12）：帧级自适应无法阻止"高于旧阈值"的噪声在 ~0.12s 内误触发起始
> （120ms 起始判定快于确认+上升的 1.5~2s）；对噪声轮的价值体现在三条：① **heal 跨轮恢复**
> （一次 ≤15s 代价，此后轮次干净）；② 慢变/边界噪声（±3dB 内）的阈值归一；③ 环境变静后
> 的缓慢回落。前两条合并的效果 = "最多坏一轮，之后自动恢复"，优于现状"每轮都坏"。

---

## 15. 风险与对策

| 风险 | 等级 | 对策 |
| --- | --- | --- |
| 语音污染抬高底噪 → 尾字截断/端点提前 | 高 | fast 只在预语音段更新（结构性排除）+ 上升确认 + 3 dB/s 限速；slow 另有 gate + 0.5 dB/s 限速 |
| 噪声突变超过 gate_db 后底噪不升（gate 悖论） | 高 | 修正 B：fast 窗口不套 gate，只排合成静音帧；污染改由更新时机防护（已并入 §4，属本稿设计而非待调参数） |
| 噪声突升/恒噪（风扇 +15~20dB 突变、或会话全程大噪声） | 高 | 冻结规则下"下一轮恢复"**不成立**：噪声在 ~0.12s 内即触发 speech_started，先于估计器 1.5~2s 的上升确认 → 该轮冻结，且下一轮预语音段再次被 0.12s 抢占（每轮都坏）。**对策 = 引擎级 heal（P2 附加项，必做）**：一轮出现"speech_started 但无端点 + 活跃占比≈100%"（或 ASR 为空）时，用该轮音频 P10 重锚 `bg_t` 并按空轮收尾——一次成本最多 15s，此后阈值=新噪声+3dB，不再误起始（P1 实施验证后据实修正） |
| 合成静音帧（-120 dB）拉低底噪 | 中 | fast/slow 均要求 `frame_dbfs > -100` 排除；`real_bytes_total` 判定真实帧双保险 |
| 跨回合状态失效（每轮从初值重来） | 高 | 修正 A：`NoiseFloorTracker` 会话级持有（已并入 §2/§8） |
| 回归风险（关闭时行为漂移） | 低 | `floor_tracker=None` 走原路径；P1 验收含"关闭时逐帧一致"回归；P4 才默认开 |
