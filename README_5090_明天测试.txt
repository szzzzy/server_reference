明天在 RTX 5090 电脑上的使用顺序

一、传输

1. 压缩整个 C:\vr_test 文件夹即可。
2. 不要把 .venv_5090_llm、.venv_5090_tts、models、third_party 放入压缩包；这些目录由菜单在 5090 电脑上创建。
3. 解压到短路径，例如 D:\vr_test，避免 Windows 路径过长。

二、启动

双击 00_menu_5090.bat。

建议顺序：

1：创建 Qwen/FunASR 环境，并安装 RTX 5090 对应的 CUDA 12.8 PyTorch。
2：下载 Qwen3-4B-Instruct-2507（主测文本大模型）。
3：下载 Qwen2.5-1.5B-Instruct（轻量对照模型）。
4：下载 Paraformer-online，用于 FunASR 到 Qwen3 联调。
5：运行 Qwen3 的 8 组文本测试。
6：运行 Qwen2.5 的同一组文本测试。
7：用今天保留的开发板 WAV 测 FunASR → Qwen3。
8：创建独立 CosyVoice2 环境并安装官方源码。
9：下载 CosyVoice2-0.5B。
10：生成 5 条 TTS 测试音频。
11：汇总现有测试结果。
13：生成排除虚拟环境、模型权重和缓存的小型传输 ZIP。

三、模型定位

Qwen3-4B-Instruct-2507 是 FunASR 识别文本后的主处理模型。
Qwen2.5-1.5B-Instruct 是低显存、低延迟对照，不是 Qwen2-Audio。
CosyVoice2-0.5B 负责将大模型回复转换为语音。

主链路：开发板麦克风 → FunASR/Paraformer-online → Qwen3 → CosyVoice2 → 扬声器。

四、结果位置

文本和指标：results_5090
TTS 音频：outputs_5090\cosyvoice2
模型权重：models

五、注意

1. 模型下载需要联网，模型权重不随本压缩包传输。
2. CosyVoice2 官方依赖对 Python 版本较敏感，本测试包固定使用 Python 3.10 的独立环境。
3. 首次运行会包含模型加载时间；正式比较时重点看首 Token、总耗时、tokens/s、首段音频和 RTF。
4. 文本回答质量仍需人工查看，程序只自动记录性能指标，不会把关键词命中冒充完整正确率。
