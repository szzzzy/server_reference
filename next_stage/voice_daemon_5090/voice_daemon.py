import csv
import json
import os
import queue as thread_queue
import sys
import threading
import time
import traceback
import wave
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
PIPELINE_DIR = PROJECT_ROOT / "next_stage" / "full_pipeline_5090"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PIPELINE_DIR))

import realtime_pipeline as rp


def append_row(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def normalize(text):
    return "".join(ch for ch in text.lower().strip() if ch not in " ，。！？,.!?；;：:\t\r\n")


def is_command(text, number):
    value = normalize(text)
    if number == 1:
        return value in {"1", "一", "幺", "自检", "开始自检", "系统自检"} or "自检" in value
    return value in {"2", "二", "两", "对话", "开始对话", "进入对话"} or "开始对话" in value or "进入对话" in value


def record_utterance(ser, api, mode, background_dbfs, max_seconds, endpoint_silence_ms=None):
    api["discard_buffered_audio"](ser)
    samples, _, endpoint = api["capture_until_endpoint"](
        ser, max_seconds=max_seconds, background_dbfs=background_dbfs,
        endpoint_silence_ms=(endpoint_silence_ms if endpoint_silence_ms is not None
                             else mode["endpoint_silence_ms"]),
        threshold_above_bg=mode["start_threshold_db"],
        endpoint_threshold_above_bg=mode["end_threshold_db"],
        endpoint_active_penalty=mode["endpoint_active_penalty"],
        voice_start_ms=mode["start_active_ms"],
        voice_start_window_ms=mode["start_window_ms"],
    )
    return samples, endpoint


def main():
    config = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    pid_file = HERE / "voice_daemon.pid"
    status_file = HERE / "voice_daemon.status.txt"
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    status_file.write_text("starting", encoding="utf-8")
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer
    from asr_eval_core import load_paraformer
    from board_serial_asr_test import (
        capture_seconds, capture_until_endpoint, discard_buffered_audio,
        open_serial, recognize, rms_dbfs, save_wav,
    )
    api = locals()
    run_dir = HERE / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_dir, tts_dir = run_dir / "audio", run_dir / "tts"
    result_csv = run_dir / "voice_daemon_results.csv"
    run_dir.mkdir(parents=True, exist_ok=True)
    mode = config["modes"][config["voice_control"]["default_mode"]]
    # Load all large models at Windows login, before the board is plugged in.
    # Plugging the board in later then only requires serial connection and
    # background calibration instead of several minutes of model loading.
    print("语音服务后台预加载模型……", flush=True)
    status_file.write_text("loading_asr", encoding="utf-8")
    asr, _, _ = load_paraformer(str(rp.resolve(PROJECT_ROOT, config["models"]["asr"])), "auto")
    status_file.write_text("loading_tts", encoding="utf-8")
    tts_process, queue = rp.start_tts_worker(config, run_dir)
    llm_path = rp.resolve(PROJECT_ROOT, config["models"]["llm"])
    status_file.write_text("loading_qwen", encoding="utf-8")
    tokenizer = AutoTokenizer.from_pretrained(str(llm_path), trust_remote_code=True, local_files_only=True)
    llm = AutoModelForCausalLM.from_pretrained(
        str(llm_path), torch_dtype="auto", device_map="auto",
        trust_remote_code=True, local_files_only=True)
    history, voice_id, turn = [], 0, 0

    prompt_texts = {
        "ready": "系统已经启动。请说你好导游唤醒我。",
        "calibrated": "背景校准完成，我已进入休眠待机。需要帮助时请说你好导游。",
        "menu": "我在，有什么需要帮助的吗？",
        "self_check": "开始自检。语音识别、文本模型、语音合成、开发板麦克风和扬声器均已加载，串口连接正常。自检完成。",
        "dialog_start": "已进入实验室环境连续对话，请开始讲话。说退出对话可以返回待机。",
        "command_retry": "没有听清。请说一开始自检，或者说二开始对话。",
        "dialog_exit": "已经退出对话，我将进入休眠待机。需要帮助时请说你好导游。"
    }
    prompt_cache = HERE / "prompt_cache"
    prompt_cache.mkdir(parents=True, exist_ok=True)

    def prompt_path(tag):
        # Version the wake response so an older, longer cached sentence is not
        # reused after upgrades.
        name = "menu_fast_v2.wav" if tag == "menu" else f"{tag}.wav"
        return prompt_cache / name

    # Prepare only the two latency-sensitive fixed prompts before a board is
    # plugged in. Avoid synthesizing the whole menu set during startup.
    status_file.write_text("preparing_fixed_prompts", encoding="utf-8")
    for cache_tag in ("menu", "calibrated"):
        cache_path = prompt_path(cache_tag)
        if not cache_path.exists():
            rp.tts_request(queue, f"cache_{cache_tag}", prompt_texts[cache_tag], cache_path)

    print("模型已就绪，等待插入开发板……", flush=True)
    status_file.write_text("models_ready_waiting_for_board", encoding="utf-8")
    while True:
        try:
            port = rp.detect_board_port(config)
            break
        except RuntimeError:
            time.sleep(1)
    print(f"已自动识别开发板：{port}", flush=True)
    ser = open_serial(port, config["serial"]["baud"])

    def play_cached_wav(path):
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                raise ValueError(f"提示音频格式不支持：{path}")
            rate = handle.getframerate()
            pcm = handle.readframes(handle.getnframes())
        rp.board_speaker_volume(ser, config["tts"]["board_volume_percent"])
        rp.board_speaker_start(ser, rate)
        ser.flush()
        time.sleep(0.03)
        rp.board_speaker_chunk(ser, pcm, rate, config["tts"]["stream_chunk_bytes"])
        rp.board_speaker_end(ser)

    def speak(text, tag):
        nonlocal voice_id
        cached = prompt_path(tag)
        if cached.exists() and prompt_texts.get(tag) == text:
            play_cached_wav(cached)
            return
        voice_id += 1
        # Generate a missing fixed prompt directly into the persistent cache.
        # This avoids blocking startup to synthesize every prompt in advance.
        wav = cached if prompt_texts.get(tag) == text else tts_dir / f"{voice_id:04d}_{tag}.wav"
        rp.tts_stream_to_board(
            queue, f"voice_{voice_id:04d}", text, wav, ser,
            config["tts"]["stream_chunk_bytes"],
            config["tts"]["board_volume_percent"])

    def transcribe(samples):
        text, first, seconds = recognize(asr, samples)
        print(f"识别：{text or '[空]'}", flush=True)
        return text.strip(), first, seconds

    def wait_and_connect_board():
        status_file.write_text("models_ready_waiting_for_board", encoding="utf-8")
        print("等待插入开发板……", flush=True)
        while True:
            try:
                detected_port = rp.detect_board_port(config)
                board_serial = open_serial(detected_port, config["serial"]["baud"])
                print(f"开发板已连接：{detected_port}", flush=True)
                return board_serial
            except Exception:
                time.sleep(1)

    def calibrate_board(board_serial):
        status_file.write_text("calibrating_background", encoding="utf-8")
        print("自动采集实验室背景，请保持安静。", flush=True)
        discard_buffered_audio(board_serial)
        background_samples, _ = capture_seconds(
            board_serial, config["voice_control"]["background_seconds"])
        measured_dbfs = rms_dbfs(background_samples)
        calibration_name = datetime.now().strftime("background_%Y%m%d_%H%M%S.wav")
        save_wav(audio_dir / calibration_name, background_samples)
        status_file.write_text("sleeping_waiting_for_wake_word", encoding="utf-8")
        return measured_dbfs

    def answer(user_text):
        nonlocal turn
        turn += 1
        messages = [{"role": "system", "content": config["conversation"]["system_prompt"]}]
        messages.extend(history[-config["conversation"]["history_turns"] * 2:])
        messages.append({"role": "user", "content": user_text})
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
        streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
        generation = dict(**inputs, streamer=streamer,
                          max_new_tokens=config["conversation"]["max_new_tokens"], do_sample=False)
        started = time.perf_counter()
        generation_thread = threading.Thread(target=llm.generate, kwargs=generation)
        generation_thread.start()
        segment_queue = thread_queue.Queue()
        response_pieces = []

        def submit_tts_segments():
            buffered = []
            chars = 0
            segment_index = 0
            for sentence in rp.sentence_chunks(streamer):
                response_pieces.append(sentence)
                buffered.append(sentence)
                chars += len(sentence)
                target_chars = (config["voice_control"].get("first_tts_segment_chars", 28)
                                if segment_index == 0 else
                                config["voice_control"].get("later_tts_segment_chars", 60))
                # Start speaking after the first complete sentence. Later
                # segments stay longer so prosody remains more continuous.
                ready = ((segment_index == 0 and len(buffered) >= 1 and chars >= 18)
                         or len(buffered) >= 2 or chars >= target_chars)
                if ready:
                    segment_index += 1
                    text = "".join(buffered).strip()
                    request_id = f"turn_{turn:03d}_{segment_index:03d}"
                    wav = tts_dir / f"{request_id}.wav"
                    stream_dir = queue / f"stream_{request_id}"
                    stream_dir.mkdir(parents=True, exist_ok=True)
                    (queue / f"request_{request_id}.json").write_text(json.dumps({
                        "id": request_id, "text": text, "output_wav": str(wav),
                        "stream_dir": str(stream_dir)
                    }, ensure_ascii=False), encoding="utf-8")
                    segment_queue.put((request_id, text, wav))
                    buffered, chars = [], 0
            if buffered:
                segment_index += 1
                text = "".join(buffered).strip()
                request_id = f"turn_{turn:03d}_{segment_index:03d}"
                wav = tts_dir / f"{request_id}.wav"
                stream_dir = queue / f"stream_{request_id}"
                stream_dir.mkdir(parents=True, exist_ok=True)
                (queue / f"request_{request_id}.json").write_text(json.dumps({
                    "id": request_id, "text": text, "output_wav": str(wav),
                    "stream_dir": str(stream_dir)
                }, ensure_ascii=False), encoding="utf-8")
                segment_queue.put((request_id, text, wav))
            generation_thread.join()
            segment_queue.put(None)

        producer = threading.Thread(target=submit_tts_segments)
        producer.start()
        tts_audio_seconds = 0.0
        segment_count = 0
        board_started = False
        while True:
            item = segment_queue.get()
            if item is None:
                break
            request_id, segment_text, wav = item
            segment_count += 1
            tts = rp.tts_stream_to_board(
                queue, request_id, segment_text, wav, ser,
                config["tts"]["stream_chunk_bytes"], config["tts"]["board_volume_percent"],
                request_precreated=True, start_board=not board_started, end_board=False)
            board_started = True
            tts_audio_seconds += float(tts.get("audio_seconds", 0) or 0)
        producer.join()
        if board_started:
            rp.board_speaker_end(ser)
        response = "".join(response_pieces).strip()
        history.extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": response}])
        append_row(result_csv, {"time": datetime.now().isoformat(timespec="seconds"),
                   "turn": turn, "recognized_text": user_text, "qwen_response": response,
                   "total_seconds": round(time.perf_counter() - started, 3),
                   "tts_audio_seconds": round(tts_audio_seconds, 3),
                   "tts_segments": segment_count})

    try:
        background_dbfs = calibrate_board(ser)
        speak(prompt_texts["calibrated"], "calibrated")
        state = "wake"
        while True:
            if state == "wake":
                limit = config["voice_control"]["wake_listen_seconds"]
            elif state == "command":
                limit = config["voice_control"]["command_listen_seconds"]
            else:
                limit = mode["max_record_seconds"]
            if state in {"wake", "command"}:
                fast_endpoint = config["voice_control"].get("wake_endpoint_silence_ms", 500)
            else:
                fast_endpoint = config["voice_control"].get("dialog_endpoint_silence_ms", 800)
            try:
                samples, endpoint = record_utterance(
                    ser, api, mode, background_dbfs, limit, fast_endpoint)
            except Exception as exc:
                print(f"开发板已断开：{exc}", flush=True)
                status_file.write_text("board_disconnected", encoding="utf-8")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = wait_and_connect_board()
                background_dbfs = calibrate_board(ser)
                speak(prompt_texts["calibrated"], "calibrated")
                history.clear()
                state = "wake"
                continue
            if not endpoint.get("speech_started"):
                continue
            text, _, _ = transcribe(samples)
            value = normalize(text)
            if not value:
                continue
            if state == "wake":
                if any(normalize(word) in value for word in config["voice_control"]["wake_words"]):
                    speak(prompt_texts["menu"], "menu")
                    status_file.write_text("awake_waiting_for_command", encoding="utf-8")
                    state = "command"
                continue
            if state == "command":
                if is_command(text, 1):
                    speak(prompt_texts["self_check"], "self_check")
                    state = "wake"
                elif is_command(text, 2):
                    speak(prompt_texts["dialog_start"], "dialog_start")
                    status_file.write_text("dialog_active", encoding="utf-8")
                    state = "dialog"
                else:
                    speak(prompt_texts["command_retry"], "command_retry")
                continue
            if any(normalize(word) in value for word in config["voice_control"]["dialog_exit_words"]):
                speak(prompt_texts["dialog_exit"], "dialog_exit")
                status_file.write_text("sleeping_waiting_for_wake_word", encoding="utf-8")
                history.clear()
                state = "wake"
                continue
            answer(text)
    finally:
        ser.close()
        shutdown = queue / "request_shutdown.json"
        shutdown.write_text(json.dumps({"id": "shutdown", "command": "shutdown"}), encoding="utf-8")
        try:
            tts_process.wait(timeout=10)
        except Exception:
            tts_process.terminate()
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        try:
            (HERE / "voice_daemon.status.txt").write_text(
                "crashed\n" + details, encoding="utf-8")
            with (HERE / "voice_daemon.log").open("a", encoding="utf-8") as handle:
                handle.write("\n===== FATAL ERROR =====\n" + details + "\n")
        finally:
            raise
