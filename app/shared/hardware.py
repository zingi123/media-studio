import os
import platform
import shutil


def hardware_report() -> str:
    lines = [f"OS: {platform.platform()}", f"CPU threads: {os.cpu_count()}"]
    try:
        import psutil
        lines.append(f"RAM: {psutil.virtual_memory().total / 1024**3:.1f} GB")
        lines.append(f"Free disk (home): {shutil.disk_usage(os.path.expanduser('~')).free / 1024**3:.0f} GB")
    except Exception:
        lines.append("RAM: install psutil to show")
    lines.append("NVIDIA GPU tool (nvidia-smi): " + ("found" if shutil.which("nvidia-smi") else "not found"))
    return "\n".join(lines)


def tool_report() -> str:
    from .ffmpeg_util import find_ffmpeg
    out = [f"FFmpeg: {find_ffmpeg() or 'NOT FOUND'}"]
    for mod, label in [("yt_dlp", "yt-dlp"), ("faster_whisper", "faster-whisper (ASR)"),
                       ("rapidocr_onnxruntime", "RapidOCR"), ("transformers", "transformers (local NLLB translation)"),
                       ("edge_tts", "edge-tts (Khmer TTS)"), ("voxcpm", "VoxCPM2 (voice cloning, GPU or CPU)"),
                       ("gradio_client", "gradio_client (voice cloning via Hugging Face online)"),
                       ("funasr", "funasr (emotion2vec - Smart Script emotion)"), ("librosa", "librosa (Smart Script loudness/pitch)")]:
        try:
            m = __import__(mod)
            out.append(f"{label}: installed {getattr(m, '__version__', '')}")
        except Exception:
            out.append(f"{label}: not installed")
    return "\n".join(out)
