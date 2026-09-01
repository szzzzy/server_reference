# -*- coding: utf-8 -*-
# ============================================================================
# TTS 模块(CosyVoice2 合成子进程)
# ----------------------------------------------------------------------------
# 背景: CosyVoice2 依赖的 torchaudio 补丁/版本与主进程(FunASR/torch)环境冲突,
#       因此 TTS 被拆成**独立子进程**,用专属 venv(.venv_5090_tts)运行:
#       主进程 → subprocess(本文件) → 目录文件 IPC → 主进程拿 PCM 推给设备。
# 职责: 轮询队列里的 request_*.json,用 CosyVoice2 zero-shot(零样本音色克隆)
#       逐句合成语音,边合成边落盘 chunk_N.pcm(流式),整段完成后写 response json。
# 调用方: engine/realtime_pipeline.start_tts_worker() 负责拉起本子进程并传参数:
#   --project-root 依赖根(默认用于推导 CosyVoice 源码路径)
#   --cosy-root    CosyVoice 源码根(third_party/CosyVoice;可显式指定)
#   --model-dir    CosyVoice2 模型目录(models/CosyVoice2-0.5B)
#   --queue-dir    队列目录(tts_queue,与主进程交互的唯一通道)
#   --prompt-wav   参考音频(音色克隆样本)+ --prompt-text 参考文本
# IPC 约定(全部为文件,原子写 + 轮询):
#   主→子: request_<id>.json {id, text, output_wav, stream_dir?, command?:"shutdown"}
#   子→主: ready.json(模型就绪) / stream_dir/chunk_N.pcm(流式音频块)
#           / response_<id>.json {ok, stream_chunks, audio_seconds, sample_rate, ...}
# ============================================================================
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio


def load_audio_compat(path, *args, **kwargs):
    """torchaudio.load 的替代实现(模块加载时被 monkey-patch 覆盖)。

    原因: CosyVoice2 源码在初始化时会调用 torchaudio.load 加载参考音频,
    但当前环境 torchaudio 与 soundfile 的 IO 后端存在兼容问题(补丁版本冲突),
    因此统一用 soundfile 读成 float32(2-D) 再转 torch 张量,绕过 torchaudio 的 IO。
    """
    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(samples.T.copy()), sample_rate


torchaudio.load = load_audio_compat


def atomic_json(path, data):
    """原子写 JSON:先写 .tmp 再 rename。

    主进程可能在任何时刻轮询本文件,直接 write_text 会让它读到"半截 JSON";
    tmp+replace 保证读到的文件要么不存在、要么完整 —— 这是无锁文件 IPC 的一致性基础。
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def atomic_bytes(path, data):
    """原子写字节(与 atomic_json 同理):chunk_N.pcm 必须先落完整再让主进程看到。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def main():
    """TTS 子进程主循环:加载一次模型,然后永续处理合成请求(单线程,按请求依次合成)。

    流程:
      ① 解析参数 → 把 CosyVoice 源码根与 Matcha-TTS 追加进 sys.path → 导入 CosyVoice2;
      ② 加载模型(CosyVoice2,关闭 jit/trt/vllm 后端,仅用原生 torch + fp16);
      ③ 写 ready.json 通知主进程"就绪"(start_tts_worker 最多等 180s);
      ④ 循环:
         扫 request_*.json(按文件名排序,取第一个)→ 解析请求
           - command=="shutdown" → 删除请求文件并退出进程;
           - 正常请求:inference_zero_shot(text, 参考文本, 参考wav, stream=True) 逐块 yield:
               * 每块 tensor("tts_speech", 1×N 采样,24kHz)→ 黏贴为 float32
                 → clamp[-1,1] → int16 → 原子写 stream_dir/chunk_N.pcm(块号从 0 递增);
               * 流式目录不存在时(仅生成完整文件)跳过落盘;
            全部块合成完 → cat 拼接 → sf.write 完整 output_wav(24kHz PCM16)
             → 原子写 response_<id>.json {ok:True, first_audio_seconds, audio_seconds,
                sample_rate, stream_chunks, ...}(供主进程计时与校验);
           - 任何异常 → 原子写 response_<id>.json {ok:False, error: repr(exc)};
           - finally:删除已处理的 request 文件(无论成功失败,避免死循环重试)。
     单个请求内部串行;多请求由主进程排队(request 文件名带序号,按序处理)。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--cosy-root", default="",
                        help="CosyVoice 源码根(默认 <project-root>/third_party/CosyVoice)")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--queue-dir", required=True)
    parser.add_argument("--prompt-wav", required=True)
    parser.add_argument("--prompt-text", required=True)
    args = parser.parse_args()

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    root = Path(args.project_root).resolve()
    cosy_root = (Path(args.cosy_root).resolve() if args.cosy_root
                 else root / "third_party" / "CosyVoice")
    sys.path.insert(0, str(cosy_root))
    sys.path.insert(0, str(cosy_root / "third_party" / "Matcha-TTS"))
    from cosyvoice.cli.cosyvoice import CosyVoice2

    queue = Path(args.queue_dir).resolve()
    queue.mkdir(parents=True, exist_ok=True)
    model = CosyVoice2(args.model_dir, load_jit=False, load_trt=False,
                       load_vllm=False, fp16=True)
    # ---- 前端特征缓存(参考音频不变):零样本合成的 prompt 特征(campplus 说话人嵌入
    # / speech_tokenizer_v2 的参考音频 token/参考 mel)每次请求都经 ONNX-CPU 重算,
    # 实测 ~0.24s/请求(首块 2.13s→1.90s,多段回答每段再省一次)。
    # 官方机制:add_zero_shot_spk → spk2info 缓存;带 zero_shot_spk_id 的
    # frontend_zero_shot 直接取缓存(third_party/CosyVoice/.../frontend.py L185)。
    SPK_ID = "main"
    try:
        model.add_zero_shot_spk(args.prompt_text, args.prompt_wav, SPK_ID)
        print("[spk-cache] 参考音频特征已缓存(add_zero_shot_spk)", flush=True)
    except Exception as cache_exc:
        SPK_ID = ""
        print(f"[spk-cache] 参考音频缓存失败,回退逐请求计算: {cache_exc}", flush=True)
    # ---- 预热(降延迟关键):正式合成前先跑一次短句,把"每段首块"的固定开销
    # (流式解码/图优化/显存分配等)在就绪阶段消化掉。
    # 预热前首块 rtf≈2.0(合成 0.9s 音频要 ~1.9s);预热后同段后续块 rtf≈0.6,
    # 首块延迟可降 1s 以上 —— 直接决定"端点→第一声"。
    try:
        for _warm in model.inference_zero_shot(
                "你好。", args.prompt_text, args.prompt_wav, stream=True,
                zero_shot_spk_id=SPK_ID):
            pass
        print("[warmup] TTS 预热完成", flush=True)
    except Exception as _wexc:
        print(f"[warmup] TTS 预热失败(忽略): {_wexc}", flush=True)
    atomic_json(queue / "ready.json", {"ready": True, "time": time.time()})

    while True:
        requests = sorted(queue.glob("request_*.json"))
        if not requests:
            time.sleep(0.05)
            continue
        request_path = requests[0]
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if request.get("command") == "shutdown":
                request_path.unlink(missing_ok=True)
                return
            started = time.perf_counter()
            first_audio = None
            chunks = []
            stream_dir_value = request.get("stream_dir")
            stream_dir = Path(stream_dir_value) if stream_dir_value else None
            if stream_dir is not None:
                stream_dir.mkdir(parents=True, exist_ok=True)
            chunk_index = 0
            for result in model.inference_zero_shot(
                request["text"], args.prompt_text, args.prompt_wav, stream=True,
                zero_shot_spk_id=SPK_ID
            ):
                if first_audio is None:
                    first_audio = time.perf_counter() - started
                tensor = result["tts_speech"].cpu()
                chunks.append(tensor)
                if stream_dir is not None:
                    samples_chunk = tensor.squeeze(0).detach().float().numpy()
                    pcm = np.clip(samples_chunk, -1.0, 1.0)
                    pcm = (pcm * 32767.0).astype("<i2").tobytes()
                    atomic_bytes(stream_dir / f"chunk_{chunk_index:06d}.pcm", pcm)
                    chunk_index += 1
            audio = torch.cat(chunks, dim=1) if chunks else torch.zeros((1, 0))
            samples = audio.squeeze(0).detach().cpu().float().numpy()
            output = Path(request["output_wav"])
            output.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(output), samples, model.sample_rate, subtype="PCM_16")
            atomic_json(queue / f"response_{request['id']}.json", {
                "ok": True,
                "id": request["id"],
                "output_wav": str(output),
                "first_audio_seconds": round(first_audio, 3) if first_audio is not None else None,
                "total_seconds": round(time.perf_counter() - started, 3),
                "audio_seconds": round(len(samples) / model.sample_rate, 3),
                "sample_rate": model.sample_rate,
                "stream_chunks": chunk_index,
            })
        except Exception as exc:
            request_id = request.get("id", "unknown") if "request" in locals() else "unknown"
            atomic_json(queue / f"response_{request_id}.json", {
                "ok": False, "id": request_id, "error": repr(exc)
            })
        finally:
            request_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
