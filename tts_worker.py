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
    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(samples.T.copy()), sample_rate


torchaudio.load = load_audio_compat


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def atomic_bytes(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--queue-dir", required=True)
    parser.add_argument("--prompt-wav", required=True)
    parser.add_argument("--prompt-text", required=True)
    args = parser.parse_args()

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    root = Path(args.project_root).resolve()
    cosy_root = root / "third_party" / "CosyVoice"
    sys.path.insert(0, str(cosy_root))
    sys.path.insert(0, str(cosy_root / "third_party" / "Matcha-TTS"))
    from cosyvoice.cli.cosyvoice import CosyVoice2

    queue = Path(args.queue_dir).resolve()
    queue.mkdir(parents=True, exist_ok=True)
    model = CosyVoice2(args.model_dir, load_jit=False, load_trt=False,
                       load_vllm=False, fp16=True)
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
                request["text"], args.prompt_text, args.prompt_wav, stream=True
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
