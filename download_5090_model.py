import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Download one model into a portable local folder.")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--hf-model-id", default="")
    parser.add_argument("--local-dir", required=True)
    parser.add_argument("--source", choices=("auto", "modelscope", "huggingface"), default="auto")
    args = parser.parse_args()

    target = Path(args.local_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    errors = []

    if args.source in ("auto", "modelscope"):
        try:
            from modelscope import snapshot_download

            print(f"Downloading from ModelScope: {args.model_id}")
            snapshot_download(args.model_id, local_dir=str(target))
            print(f"Model ready: {target}")
            return
        except Exception as exc:
            errors.append(f"ModelScope: {exc}")
            if args.source == "modelscope":
                raise

    try:
        from huggingface_hub import snapshot_download

        hf_model_id = args.hf_model_id or args.model_id
        print(f"Downloading from Hugging Face: {hf_model_id}")
        snapshot_download(repo_id=hf_model_id, local_dir=str(target))
        print(f"Model ready: {target}")
    except Exception as exc:
        errors.append(f"Hugging Face: {exc}")
        raise RuntimeError("Model download failed.\n" + "\n".join(errors)) from exc


if __name__ == "__main__":
    main()
