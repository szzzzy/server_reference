import platform
import sys


def main():
    print(f"Python: {sys.version.split()[0]} ({platform.architecture()[0]})")
    try:
        import torch

        print(f"PyTorch: {torch.__version__}")
        print(f"CUDA available: {torch.cuda.is_available()}")
        print(f"CUDA runtime: {torch.version.cuda}")
        if torch.cuda.is_available():
            print(f"GPU: {torch.cuda.get_device_name(0)}")
            total = torch.cuda.get_device_properties(0).total_memory / 1024**3
            print(f"GPU memory: {total:.2f} GB")
    except Exception as exc:
        print(f"PyTorch check failed: {exc}")


if __name__ == "__main__":
    main()
