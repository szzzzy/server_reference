import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
import winsound
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 本模块位于 <包根>/engine/ 下,一级父目录即包根(即 models/venv 的 fallback 解析基准)
PROJECT_ROOT = HERE.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))


def resolve(root, value):
    """路径解析:绝对路径原样返回;相对路径拼在 root(解析基准)下 —— 统一所有依赖的定位。"""
    path = Path(value)
    return path if path.is_absolute() else root / path


def detect_board_port(config):
    from serial.tools import list_ports
    configured = str(config["serial"].get("port", "")).upper()
    ports = list(list_ports.comports())
    for item in ports:
        if item.vid == 0x303A and item.pid == 0x1001:
            return item.device
    for item in ports:
        text = f"{item.description} {item.manufacturer} {item.hwid}".lower()
        if "esp32" in text or "usb jtag" in text or "usb-serial" in text:
            return item.device
    for item in ports:
        if item.device.upper() == configured:
            return item.device
    available = ", ".join(item.device for item in ports) or "无"
    raise RuntimeError(f"没有检测到ESP32-S3开发板。当前串口：{available}。请重新插拔开发板USB数据线。")


def select_mode(config):
    keys = list(config["modes"])
    print("\n选择环境模式：")
    for index, key in enumerate(keys, 1):
        print(f" {index}. {config['modes'][key]['name']}")
    while True:
        value = input("请输入数字：").strip()
        if value.isdigit() and 1 <= int(value) <= len(keys):
            key = keys[int(value) - 1]
            return key, config["modes"][key]
        print("输入无效。")


def find_reference(config, root=None):
    """查找 TTS 音色克隆的"参考音频 + 参考文本"(zero-shot 的音色来源)。

    优先取 config.tts.reference_manifest(如 tts_roles_5090/results/reference_manifest.json)
    里 role(如 museum_female)对应的条目:音频路径在 manifest 同级或 root 下都可能,逐个试;
    找不到则回退 config.tts.fallback_reference_wav/text(CosyVoice 自带音色,保证能跑)。
    root: 依赖根(角色音色/备选音频的解析基准);None = 模块所在包根。
    返回: (参考 wav 路径, 参考文本)。
    """
    # root: 依赖根(角色音色/备选参考音频的解析基准);None = 本模块所在项目根
    base = Path(root) if root else PROJECT_ROOT
    tts = config["tts"]
    manifest = resolve(base, tts["reference_manifest"])
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        role = data.get(tts["role"])
        if role:
            wav = resolve(base, role["audio"])
            if not wav.exists() and not Path(role["audio"]).is_absolute():
                wav = manifest.parent / role["audio"]
            if wav.exists():
                return wav, role.get("text", tts["fallback_reference_text"])
    return resolve(base, tts["fallback_reference_wav"]), tts["fallback_reference_text"]


def run_import_check(python, modules):
    if not python.exists():
        return False, f"缺少环境：{python}"
    code = "; ".join(f"import {module}" for module in modules)
    result = subprocess.run(
        [str(python), "-c", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=PROJECT_ROOT
    )
    if result.returncode == 0:
        return True, "依赖导入正常"
    detail = (result.stderr or result.stdout).strip().splitlines()
    return False, detail[-1] if detail else "依赖导入失败"


def self_check(config):
    checks = []

    def add(name, ok, detail):
        checks.append(ok)
        print(f"[{'通过' if ok else '失败'}] {name}：{detail}")

    llm_python = PROJECT_ROOT / ".venv_5090_llm" / "Scripts" / "python.exe"
    tts_python = PROJECT_ROOT / ".venv_5090_tts" / "Scripts" / "python.exe"
    ok, detail = run_import_check(
        llm_python, ["torch", "transformers", "funasr", "serial", "soundfile"]
    )
    add("LLM/FunASR环境", ok, detail)
    ok, detail = run_import_check(
        tts_python, ["torch", "torchaudio", "soundfile", "numpy"]
    )
    add("CosyVoice2环境", ok, detail)

    for label, value in (
        ("Paraformer模型", config["models"]["asr"]),
        ("Qwen3模型", config["models"]["llm"]),
        ("CosyVoice2模型", config["models"]["tts"]),
        ("CosyVoice源码", "third_party/CosyVoice"),
    ):
        path = resolve(PROJECT_ROOT, value)
        add(label, path.exists(), str(path))

    prompt_wav, _ = find_reference(config)
    add("TTS参考音频", prompt_wav.exists(), str(prompt_wav))

    expected_modes = {"quiet", "lab", "noise"}
    actual_modes = set(config.get("modes", {}))
    add("三种VAD模式", expected_modes <= actual_modes,
        "、".join(config.get("modes", {}).get(key, {}).get("name", key)
                  for key in ("quiet", "lab", "noise") if key in actual_modes))

    try:
        result = subprocess.run(
            [str(llm_python), "-c",
             "from serial.tools.list_ports import comports; print(','.join(p.device for p in comports()))"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
        )
        ports = result.stdout.strip()
        add("串口检测", result.returncode == 0 and bool(ports), ports or "未发现串口，请检查开发板USB连接和驱动")
    except Exception as exc:
        add("串口检测", False, repr(exc))

    print("\n自检结论：" + ("可以开始完整链路联调。" if all(checks) else "存在失败项，请先按上面的路径补齐。"))
    return 0 if all(checks) else 1


def start_tts_worker(config, run_dir):
    """拉起 TTS 合成子进程(engine/tts_worker.py,TTS 模块的服务端入口),等它就绪。

    参数解析(均可经 config 覆盖,便于自包含/换机):
      config["project_root"]                   = 依赖根(models/venv/third_party 解析基准)
      config["venvs"]["tts"]                   = TTS 环境 python.exe(绝对路径,默认 <root>/.venv_5090_tts)
      config["third_party"]["cosy_voice_root"] = CosyVoice 源码根(默认 <root>/third_party/CosyVoice)
      config["models"]["tts"]                  = CosyVoice2 模型目录(相对 root/绝对)
      config["tts"]["reference_manifest"/...]  = 由 find_reference 解析出参考音频与文本
    启动前检查 5 个必需路径(失败抛 FileNotFoundError,列出缺什么):
      tts_python / tts_worker.py / model_dir / prompt_wav / cosy_root。
    随后 subprocess.Popen 拉起(无控制台捕获,日志直接进 TTS 环境),并等待
    queue/ready.json 出现(限 180s,超时 terminate 后抛 TimeoutError)。
    返回: (process, queue 目录 Path):进程句柄 + 供主进程写请求的队列目录。
    """
    # 依赖位置均可经 config 显式覆盖(绝对路径优先),便于自包含打包:
    #   config["project_root"]                   = 依赖根(models/venv/third_party 的解析基准)
    #   config["venvs"]["tts"]                   = TTS 环境 python.exe(绝对路径)
    #   config["third_party"]["cosy_voice_root"] = CosyVoice 源码根(绝对路径)
    base_root = Path(config.get("project_root") or PROJECT_ROOT)
    venvs = config.get("venvs") or {}
    tts_python = Path(venvs.get("tts") or base_root / ".venv_5090_tts" / "Scripts" / "python.exe")
    worker = HERE / "tts_worker.py"
    queue = run_dir / "tts_queue"
    queue.mkdir(parents=True, exist_ok=True)
    prompt_wav, prompt_text = find_reference(config, base_root)
    model_dir = resolve(base_root, config["models"]["tts"])
    third_party = config.get("third_party") or {}
    cosy_root = Path(third_party.get("cosy_voice_root")
                     or base_root / "third_party" / "CosyVoice")
    required = [tts_python, worker, model_dir, prompt_wav, cosy_root]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("TTS缺少文件：\n" + "\n".join(missing))
    process = subprocess.Popen([
        str(tts_python), str(worker),
        "--project-root", str(base_root),
        "--cosy-root", str(cosy_root),
        "--model-dir", str(model_dir),
        "--queue-dir", str(queue),
        "--prompt-wav", str(prompt_wav),
        "--prompt-text", prompt_text,
    ], cwd=PROJECT_ROOT)
    ready = queue / "ready.json"
    print("正在加载CosyVoice2...")
    deadline = time.time() + 180
    while time.time() < deadline:
        if ready.exists():
            print("CosyVoice2已就绪。")
            return process, queue
        if process.poll() is not None:
            raise RuntimeError(f"TTS工作进程退出，代码 {process.returncode}")
        time.sleep(0.2)
    process.terminate()
    raise TimeoutError("CosyVoice2加载超过180秒。")


def tts_request(queue, request_id, text, output_wav, timeout=180):
    """同步 TTS 请求:写 request → 轮询 response → 返回结果(非流式,整段合成完才返回)。

    与流式版 tts_stream_to_board 的区别:不设 stream_dir,worker 只产出完整 wav
    (用于缓存提示音等需要"完整文件"的场景);超时抛 TimeoutError,失败抛 RuntimeError。
    """
    request = queue / f"request_{request_id}.json"
    response = queue / f"response_{request_id}.json"
    request.write_text(json.dumps({
        "id": request_id, "text": text, "output_wav": str(output_wav)
    }, ensure_ascii=False), encoding="utf-8")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if response.exists():
            data = json.loads(response.read_text(encoding="utf-8"))
            response.unlink(missing_ok=True)
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "TTS失败"))
            return data
        time.sleep(0.05)
    raise TimeoutError("TTS生成超时。")


# ============================================================================
# 板卡串口推流协议(本地板卡链路用;服务器 WSS 链路对应 real_engine._stream_tts_segment)
#   命令均为 4 字节 ASCII + 4 字节小端参数:
#     SPKS <sample_rate>  开播:必须先于一切 PCM 发出(固件未收到会丢弃 PCM)
#     SPKV <percent>      音量 0~100
#     SPKD <len> <bytes>  一包音频数据(≤2048B 小包)
#     SPKE                结束播报
# ============================================================================
def serial_write_all(ser, data):
    """串口全量写出(循环写直到写完;写 0 字节视为错误,防无限循环)。"""
    view = memoryview(data)
    while view:
        written = ser.write(view)
        if written is None or written <= 0:
            raise IOError("向开发板写入音频失败")
        view = view[written:]


def board_speaker_start(ser, sample_rate):
    """开播命令 SPKS <采样率>(必须最早发出,否则板卡丢弃后续 PCM)。"""
    serial_write_all(ser, b"SPKS" + int(sample_rate).to_bytes(4, "little"))


def board_speaker_volume(ser, percent):
    """设置板卡音量 SPKV <percent 0~100>(越界自动钳制)。"""
    percent = max(0, min(100, int(percent)))
    serial_write_all(ser, b"SPKV" + percent.to_bytes(4, "little"))


def board_speaker_chunk(ser, pcm, sample_rate=24000, max_bytes=2048):
    """按 ≤max_bytes 字节分块发送一包音频(SPKD),并按播放节奏节流。

    节流系数 0.88:睡"这段音频时长"的 88%,即以 ≈1.14×实时发送 ——
    让板卡缓冲里始终攒一点音频(100% 会因网络/串口抖动欠载出现拖音,过快则溢缓冲)。
    奇数长度自动截掉最后 1 字节(保证 16bit 对齐)。
    """
    if len(pcm) % 2:
        pcm = pcm[:-1]
    for offset in range(0, len(pcm), max_bytes):
        part = pcm[offset:offset + max_bytes]
        serial_write_all(ser, b"SPKD" + len(part).to_bytes(4, "little") + part)
        # Keep a small amount of audio queued on the board. Sleeping longer
        # than the PCM duration creates I2S underruns, heard as dragging.
        ser.flush()
        time.sleep((len(part) / 2) / sample_rate * 0.88)


def board_speaker_end(ser):
    """结束播报 SPKE(通知板卡播放已完成,可回到静默/播放队列就绪)。"""
    serial_write_all(ser, b"SPKE" + (0).to_bytes(4, "little"))
    ser.flush()


def tts_stream_to_board(queue, request_id, text, output_wav, ser, chunk_bytes=4096,
                        volume_percent=100, timeout=180, request_precreated=False,
                        start_board=True, end_board=True):
    """流式 TTS + 串口推播(本地板卡链路版):提交请求后边等边发,发完收尾。

    流程:
      ① 若 request_precreated=False,先写 request_<id>.json(带 stream_dir);
      ② 轮询 stream_dir/chunk_N.pcm:
           - 第一个块到达 → 首次: SPKV 音量 + SPKS 24000 → 记 first_chunk_seconds;
           - 每块 board_speaker_chunk 分块发出(≤2048B/包,0.88 节流),删除该块,N+1;
      ③ 出现 response_<id>.json:
           - 校验已发块数 == response.stream_chunks,不够则等(worker 只是信号,块还没齐);
           - 齐了 → 删 response,若 start_board/end_board 按参数发 SPKE;
           - 返回数据(追加 board_first_chunk_seconds/board_bytes_sent/已播秒数供记录)。
      ④ 超时(timeout 默认 180s):已开播则补发 SPKE 并抛 TimeoutError。
    request_precreated: 由调用方(V5 daemon 切句线程)预创建 request 时置 True,
    避免"先写请求再立刻被推流函数重复写"。
    start_board/end_board: 跨段连续播控制 —— 首段发 SPKS、末段发 SPKE,中间段都不发,
    避免段间出现开播/停止的咔哒与延迟。
    """
    request = queue / f"request_{request_id}.json"
    response = queue / f"response_{request_id}.json"
    stream_dir = queue / f"stream_{request_id}"
    stream_dir.mkdir(parents=True, exist_ok=True)
    if not request_precreated:
        request.write_text(json.dumps({
            "id": request_id, "text": text, "output_wav": str(output_wav),
            "stream_dir": str(stream_dir),
        }, ensure_ascii=False), encoding="utf-8")
    deadline = time.time() + timeout
    next_index = 0
    started = False
    first_chunk_seconds = None
    board_bytes_sent = 0
    request_started = time.perf_counter()
    while time.time() < deadline:
        chunk_path = stream_dir / f"chunk_{next_index:06d}.pcm"
        if chunk_path.exists():
            pcm = chunk_path.read_bytes()
            if not started:
                if start_board:
                    board_speaker_volume(ser, volume_percent)
                    board_speaker_start(ser, 24000)
                    ser.flush()
                    time.sleep(0.03)
                started = True
                first_chunk_seconds = time.perf_counter() - request_started
            board_speaker_chunk(ser, pcm, 24000, min(int(chunk_bytes), 2048))
            board_bytes_sent += len(pcm)
            chunk_path.unlink(missing_ok=True)
            next_index += 1
            continue
        if response.exists():
            data = json.loads(response.read_text(encoding="utf-8"))
            expected = int(data.get("stream_chunks", next_index))
            if next_index < expected:
                time.sleep(0.01)
                continue
            response.unlink(missing_ok=True)
            if started and end_board:
                board_speaker_end(ser)
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "TTS失败"))
            data["board_first_chunk_seconds"] = first_chunk_seconds
            data["board_bytes_sent"] = board_bytes_sent
            data["board_audio_seconds_sent"] = round(board_bytes_sent / 2 / 24000, 3)
            print(f"\n[扬声器] 已发送 {board_bytes_sent} 字节，约 {board_bytes_sent / 2 / 24000:.2f} 秒音频")
            return data
        time.sleep(0.01)
    if started and end_board:
        board_speaker_end(ser)
    raise TimeoutError("TTS流式生成或开发板播放超时。")


def sentence_chunks(streamer):
    """流式消费 LLM 输出,按标点切句(供 LLM 切句线程用;V5/V5daemon 与服务器共用)。

    切句规则(决定"边想边说"粒度,与 real_engine._sentence_chunks 一致):
      ① 优先按标点:。！？!?；; 或换行 \n → 切出一句(含标点);
      ② 长句保护:缓冲 ≥48 字仍无句号 → 取 20~48 字区间最后出现的 ,、：, 处切
        (区间内无逗号则硬切 48 字),避免长时间憋着不出声;
      ③ 结束收尾:生成完剩余非空缓冲作为最后一句。
    note: 本版本带控制台实时打印(piece 原样输出),用于本地命令行链路;
    服务器(GBK/无控制台)用 real_engine 内重建的无打印版本。
    """
    buffer = ""
    for piece in streamer:
        print(piece, end="", flush=True)
        buffer += piece
        while True:
            match = re.search(r"[。！？!?；;\n]", buffer)
            if not match:
                break
            end = match.end()
            sentence = buffer[:end].strip()
            buffer = buffer[end:]
            if sentence:
                yield sentence
        if len(buffer) >= 48:
            split_at = max(buffer.rfind(mark, 0, 48) for mark in "，、：,")
            if split_at < 20:
                split_at = 48
            else:
                split_at += 1
            sentence = buffer[:split_at].strip()
            buffer = buffer[split_at:]
            if sentence:
                yield sentence
    if buffer.strip():
        yield buffer.strip()


def append_row(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.self_check:
        return self_check(config)

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer
    from asr_eval_core import load_paraformer
    from board_serial_asr_test import (
        capture_seconds, capture_until_endpoint, discard_buffered_audio,
        open_serial, recognize, rms_dbfs, save_wav,
    )

    mode_key, mode = select_mode(config)
    port = detect_board_port(config)
    print(f"已自动识别开发板串口：{port}")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = HERE / "runs" / run_id
    audio_dir = run_dir / "audio"
    tts_dir = run_dir / "tts"
    result_csv = run_dir / "pipeline_results.csv"
    run_dir.mkdir(parents=True, exist_ok=True)

    tts_process = None
    queue = None
    ser = None
    history = []
    try:
        asr_model = resolve(PROJECT_ROOT, config["models"]["asr"])
        llm_model = resolve(PROJECT_ROOT, config["models"]["llm"])
        print("正在加载FunASR...")
        asr, _, _ = load_paraformer(str(asr_model), "auto")
        print("FunASR已就绪，正在加载CosyVoice2...")
        tts_process, queue = start_tts_worker(config, run_dir)
        print("CosyVoice2已就绪，正在加载Qwen3...")
        tokenizer = AutoTokenizer.from_pretrained(str(llm_model), trust_remote_code=True, local_files_only=True)
        llm = AutoModelForCausalLM.from_pretrained(
            str(llm_model), torch_dtype="auto", device_map="auto",
            trust_remote_code=True, local_files_only=True
        )
        print("FunASR、Qwen3和CosyVoice2均已就绪。")
        ser = open_serial(port, config["serial"]["baud"])

        input(f"\n当前模式：{mode['name']}。保持安静，按回车录制4秒背景...")
        discard_buffered_audio(ser)
        background, _ = capture_seconds(ser, 4)
        background_dbfs = rms_dbfs(background)
        save_wav(audio_dir / "background.wav", background)
        print(f"背景：{background_dbfs:.2f} dBFS")

        turn = 0
        while True:
            command = input("\n按回车开始一轮对话；输入 q 退出：").strip().lower()
            if command == "q":
                break
            turn += 1
            discard_buffered_audio(ser)
            print("现在请讲话，说完后保持安静...")
            captured_at = time.perf_counter()
            samples, _, endpoint = capture_until_endpoint(
                ser,
                max_seconds=mode["max_record_seconds"],
                background_dbfs=background_dbfs,
                endpoint_silence_ms=mode["endpoint_silence_ms"],
                threshold_above_bg=mode["start_threshold_db"],
                endpoint_threshold_above_bg=mode["end_threshold_db"],
                endpoint_active_penalty=mode["endpoint_active_penalty"],
                voice_start_ms=mode["start_active_ms"],
                voice_start_window_ms=mode["start_window_ms"],
            )
            vad_seconds = time.perf_counter() - captured_at
            input_wav = audio_dir / f"turn_{turn:02d}_input.wav"
            save_wav(input_wav, samples)

            asr_started = time.perf_counter()
            recognized, first_partial, asr_seconds = recognize(asr, samples)
            asr_wall = time.perf_counter() - asr_started
            print(f"\nASR：{recognized or '[空]'}")
            if not recognized.strip():
                print("未识别到有效文本，本轮不发送给Qwen。")
                continue

            messages = [{"role": "system", "content": config["conversation"]["system_prompt"]}]
            messages.extend(history[-config["conversation"]["history_turns"] * 2:])
            messages.append({"role": "user", "content": recognized})
            prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
            streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
            generation = dict(
                **inputs, streamer=streamer,
                max_new_tokens=config["conversation"]["max_new_tokens"],
                do_sample=False,
            )
            llm_started = time.perf_counter()
            thread = threading.Thread(target=llm.generate, kwargs=generation)
            thread.start()
            print("Qwen：", end="", flush=True)
            response_parts = []
            first_text_seconds = None
            first_audio_seconds = None
            tts_total = 0.0
            for sentence in sentence_chunks(streamer):
                if first_text_seconds is None:
                    first_text_seconds = time.perf_counter() - llm_started
                response_parts.append(sentence)
            thread.join()
            print()
            response_text = "".join(response_parts).strip()
            wav = tts_dir / f"turn_{turn:02d}.wav"
            if config["tts"].get("output") == "board_speaker":
                tts = tts_stream_to_board(
                    queue, f"{turn:02d}", response_text, wav, ser,
                    config["tts"].get("stream_chunk_bytes", 1024),
                    config["tts"].get("board_volume_percent", 100),
                )
            else:
                tts = tts_request(queue, f"{turn:02d}", response_text, wav)
            tts_total = tts["total_seconds"]
            first_audio_seconds = time.perf_counter() - llm_started
            if config["tts"]["play_audio"]:
                winsound.PlaySound(str(wav), winsound.SND_FILENAME)
            llm_total = time.perf_counter() - llm_started
            history.extend([
                {"role": "user", "content": recognized},
                {"role": "assistant", "content": response_text},
            ])
            append_row(result_csv, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "turn": turn, "mode": mode_key,
                "background_dbfs": round(background_dbfs, 2),
                "audio_seconds": round(len(samples) / 16000, 3),
                "vad_speech_started": endpoint["speech_started"],
                "vad_endpoint_triggered": endpoint["endpoint_triggered"],
                "vad_seconds": round(vad_seconds, 3),
                "recognized_text": recognized,
                "asr_first_partial_seconds": first_partial,
                "asr_seconds": round(asr_seconds, 3),
                "asr_wall_seconds": round(asr_wall, 3),
                "qwen_first_text_seconds": round(first_text_seconds, 3) if first_text_seconds is not None else "",
                "qwen_tts_first_audio_seconds": round(first_audio_seconds, 3) if first_audio_seconds is not None else "",
                "qwen_tts_total_seconds": round(llm_total, 3),
                "tts_generation_seconds_sum": round(tts_total, 3),
                "qwen_response": response_text,
                "input_wav": str(input_wav),
            })
            print(f"本轮结果：{result_csv}")
    finally:
        if ser is not None:
            ser.close()
        if tts_process is not None and queue is not None:
            try:
                shutdown = queue / "request_shutdown.json"
                shutdown.write_text(json.dumps({"id": "shutdown", "command": "shutdown"}), encoding="utf-8")
                tts_process.wait(timeout=10)
            except Exception:
                tts_process.terminate()


if __name__ == "__main__":
    raise SystemExit(main() or 0)
