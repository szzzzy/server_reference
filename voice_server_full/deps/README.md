# deps/ —— 外部大依赖说明（模型 / Python 环境 / CosyVoice 源码）

本包**代码、配置、证书、发布物全部自包含**；以下大体积依赖按 `server/config.json → voice.real.deps_root`
统一解析。默认 `deps_root` 指向原 5090 项目根（本包立即可用，无重复占用）。

## 依赖清单（都在 deps_root 下）

| 依赖 | 路径(相对 deps_root) | 体积量级 | 缺省影响 |
|---|---|---|---|
| ASR 模型 `paraformer-zh-streaming` | `models/paraformer-zh-streaming/` | ~1 GB | ASR 无法推理 |
| LLM 模型 `Qwen3-4B-Instruct-2507` | `models/Qwen3-4B-Instruct-2507/` | ~8 GB | LLM 无法推理 |
| TTS 模型 `CosyVoice2-0.5B` | `models/CosyVoice2-0.5B/` | ~1 GB | TTS 无法推理 |
| CosyVoice 源码 | `third_party/CosyVoice/` | ~0.5 GB | TTS 无法启动 |
| GPU 推理环境 | `.venv_5090_llm/` | 若干 GB | `--voice-mode real` 无法启动 |
| TTS 环境 | `.venv_5090_tts/` | 若干 GB | 同上 |
| TTS 角色/参考音色 | `tts_roles_5090/`(可选) | 数十 MB | 回退 CosyVoice 自带 `zero_shot_prompt.wav` |
| TTS 基础配置(base_config) | `next_stage/voice_sleep_v5_5090/config.json`(可选) | 1 KB | 用包引擎默认参数 |
| 板卡模拟器参考音频 | 包内 `samples/`(已自带) | 3.6 MB | 已自包含 |

## 模式 A：指向原项目根（默认，立即用）

无需任何操作。`deps_root` 已是绝对路径：
`C:/Users/user/Desktop/vr_test_5090_20260819_224201`

## 模式 B：完全自包含（搬到别的机器 / 脱离原项目）

1. 把上述依赖拷贝/移动到本包根同级目录，例如：
   - `voice_server_full\models\`（存放三个模型目录）
   - `voice_server_full\.venv_5090_llm\`、`voice_server_full\.venv_5090_tts\`
   - `voice_server_full\third_party\CosyVoice\`
   - `voice_server_full\tts_roles_5090\`（可选）
   - `voice_server_full\next_stage\voice_sleep_v5_5090\config.json`（可选）
2. 修改 `server\config.json`：
   ```json
   "voice": { "real": { "deps_root": "." } }
   ```
   （相对路径按本包根解析；也可用绝对路径。）

> venv 说明：venv 内含绝对路径（pyvenv.cfg 的 home 指向原 Python 安装），跨机器迁移建议
> 在目标机按 `requirements_5090_llm.txt` / `requirements_base.txt` 重建；模型目录与
> `third_party/CosyVoice` 纯数据/源码，可整目录复制。
