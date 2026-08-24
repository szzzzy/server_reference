本测试包已经改成不依赖 conda。

你现在只需要双击：

00_menu.bat

如果安装 PyTorch 时报 WinError 206 或“文件名或扩展名太长”，先双击：

COPY_TO_SHORT_PATH.bat

它会把测试包复制到 C:\vr_test。之后到 C:\vr_test 里运行 00_menu.bat，再按 1、3、4 顺序测试。

然后按顺序输入：

1：自动创建本文件夹里的 .venv 虚拟环境，并安装基础依赖
2：检查 Python、GPU、nvidia-smi
3：安装 PyTorch、FunASR / ModelScope
4：测试 Paraformer-online 语音识别（主测，人机对话优先）
4p：生成短句/命令词 ASR 测试音频和标准文本
4a：批量测试 ASR 准确率和命令词命中率
4b：连续重复测试 ASR 稳定性
4c：模拟实时话音识别 baseline，按 20ms 小包回放，服务器累计约 600ms 送入 ASR
4d：实时参数扫描，对比 20ms/40ms 小包和 300ms/600ms/900ms ASR 窗口
4e：真人麦克风 ASR 闭环测试，1m 正前方
4f：真人麦克风 ASR 闭环测试，2m 正前方
4g：开发板麦克风串口 PCM -> ASR 闭环测试，1m 正前方
4h：开发板麦克风串口 PCM -> ASR 闭环测试，2m 正前方
4s：测试 SenseVoiceSmall 语音识别（轻量对照）

说明：

1. .venv 是本文件夹里的独立 Python 环境，不会污染电脑原来的 Python。
2. 8GB 显存电脑可以先测 Paraformer-online、SenseVoiceSmall、环境检查和 HTTP 接口延迟。安装 PyTorch 会占用几 GB 硬盘空间，但不会长期占用显存。
3. Qwen3、CosyVoice2、完整流水线建议在 5090 或阿里云 GPU 上部署后，再用菜单 5、6、7 测接口。
4. 测试结果保存在 results 文件夹。
5. 今天主测方案是 Paraformer-online -> Qwen3 -> CosyVoice2。SenseVoiceSmall 只做轻量对照，Qwen2-Audio 是后续端到端对照候选，不是今天主测。

ASR 这一块：

先跑 4，确认 Paraformer-online 能运行；
再跑 4p，生成短句/命令词测试音频；
再跑 4a，生成准确率和命令词命中率结果；
再跑 4b，连续跑 5 次看稳定性；
再跑 4c，得到今天要补充的实时话音识别 baseline；
再跑 4d，对比不同分片/缓存参数对延迟和识别结果的影响；
如果要测真人读话，就跑 4e 和 4f。程序会先录 10 秒背景噪声，然后提示你逐句朗读，结果保存在 results/live_human_mic_asr_results.csv，录音保存在 recordings 文件夹。

真正用开发板麦克风测试时，先烧录 espidf-mic-pcm-stream 工程。烧录后回到本菜单：
4g 测 1m，4h 测 2m。程序会读取开发板串口 PCM，送入 Paraformer-online，结果保存在 results/board_mic_asr_results.csv，开发板麦克风录音保存在 recordings 文件夹。

今天要区分两种测试：

1. 文件输入基准：固定音频文件直接送 ASR，验证模型基础准确率、RTF 和运行显存。
2. 实时流基准：仍使用同一段音频，但按 20ms/40ms 小包模拟 ESP32 实时上传，服务器累计 300ms/600ms/900ms 再送 ASR，验证首个识别结果延迟、最终结果延迟、参数对体验的影响。
