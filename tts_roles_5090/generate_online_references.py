import asyncio
import json
from pathlib import Path

import edge_tts
from edge_tts.exceptions import NoAudioReceived


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "outputs" / "online_role_references"
RESULT_DIR = HERE / "results"
TEXT = "欢迎参观博物馆，我将为您介绍这件珍贵文物。"

ROLES = [
    {"id": "museum_female", "role_name": "女声博物馆导览", "gender": "female",
     "voices": ["zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural"],
     "rate": "+0%", "volume": "+10%"},
    {"id": "museum_male", "role_name": "男声正式讲解", "gender": "male",
     "voices": ["zh-CN-YunxiNeural", "zh-CN-YunyangNeural", "zh-CN-YunjianNeural"],
     "rate": "-5%", "volume": "+10%"},
    {"id": "accessibility_slow", "role_name": "无障碍慢速讲解", "gender": "female",
     "voices": ["zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural"],
     "rate": "-25%", "volume": "+10%"},
]


async def generate_with_fallback(role, path):
    candidates = role["voices"]
    errors = []
    for voice in candidates:
        print(f"Trying {voice} (20 second timeout)...", flush=True)
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(
                    TEXT, voice, rate=role["rate"], volume=role["volume"]
                ).save(str(path)),
                timeout=20,
            )
            if path.exists() and path.stat().st_size > 1000:
                return voice
            errors.append(f"{voice}: empty output")
        except (NoAudioReceived, asyncio.TimeoutError, OSError) as exc:
            errors.append(f"{voice}: {type(exc).__name__}: {exc}")
            print(f"  Failed, switching voice: {type(exc).__name__}", flush=True)
    raise RuntimeError("All candidate voices failed: " + " | ".join(errors))


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for role in ROLES:
        path = OUTPUT_DIR / f"{role['id']}_reference.mp3"
        voice = await generate_with_fallback(role, path)
        manifest[role["id"]] = {
            **role,
            "source_mode": "Microsoft Edge online neural voice",
            "source": voice,
            "audio": str(path),
            "text": TEXT,
            "license_note": "Research test reference only; verify service and redistribution terms before product deployment."
        }
        print(f"[{role['role_name']}] {voice} {role['rate']} -> {path}")
    out = RESULT_DIR / "reference_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Reference manifest:", out)


if __name__ == "__main__":
    asyncio.run(main())
