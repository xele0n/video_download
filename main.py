from pathlib import Path
from typing import Optional, Any
import argparse
import asyncio
from dataclasses import dataclass, field
import json
import os
import sys
import threading
import time
from uuid import uuid4

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Optional Google AdSense configuration (for the web UI).
# Set these environment variables in your shell or process manager:
#   ADSENSE_CLIENT_ID  e.g. "ca-pub-7674438363848466"
#   ADSENSE_SLOT_ID    e.g. "1234567890"
ADSENSE_CLIENT_ID = os.getenv("ADSENSE_CLIENT_ID")
ADSENSE_SLOT_ID = os.getenv("ADSENSE_SLOT_ID")

app = FastAPI(title="Video Downloader")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    error: Optional[str] = None,
    info: Optional[str] = None,
    url: Optional[str] = None,
    file_format: Optional[str] = None,
):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "error": error,
            "info": info,
            "url": url,
            "file_format": file_format,
            "adsense_client_id": ADSENSE_CLIENT_ID,
            "adsense_slot_id": ADSENSE_SLOT_ID,
        },
    )


@dataclass
class DownloadJob:
    id: str
    url: str
    file_format: str
    status: str = "queued"  # queued|extracting|downloading|processing|finished|error
    stage: str = "Queued"
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


_JOBS: dict[str, DownloadJob] = {}
_JOBS_LOCK = threading.Lock()


def _set_job(job_id: str, **updates: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        for k, v in updates.items():
            setattr(job, k, v)
        job.updated_at = time.time()


def _get_job(job_id: str) -> Optional[DownloadJob]:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _resolve_final_path(filename: str) -> Path:
    path = Path(filename)
    # Postprocessors (e.g. FFmpegExtractAudio, merge) can change the final extension.
    if not path.exists():
        stem = path.stem
        for ext in (".mp3", ".mp4", ".m4a", ".webm"):
            candidate = path.parent / f"{stem}{ext}"
            if candidate.exists():
                return candidate
    return path


def _job_download_hook(job_id: str, d: dict) -> None:
    status = d.get("status")
    if status == "downloading":
        downloaded = int(d.get("downloaded_bytes") or 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        total_int = int(total) if isinstance(total, (int, float)) else None
        progress = (downloaded / total_int) if total_int and total_int > 0 else 0.0
        _set_job(
            job_id,
            status="downloading",
            stage="Downloading",
            downloaded_bytes=downloaded,
            total_bytes=total_int,
            progress=max(0.0, min(1.0, progress)),
        )
    elif status == "finished":
        _set_job(job_id, status="processing", stage="Processing", progress=1.0)


def _job_postprocessor_hook(job_id: str, d: dict) -> None:
    # Best-effort: yt-dlp may call this for merges/conversions.
    status = d.get("status")
    if status == "started":
        _set_job(job_id, status="processing", stage="Processing")
    elif status == "finished":
        _set_job(job_id, status="processing", stage="Finalizing")


def _run_download_job(job_id: str) -> None:
    job = _get_job(job_id)
    if not job:
        return

    try:
        _set_job(job_id, status="extracting", stage="Extracting video info", progress=0.0)

        output_template = str(
            DOWNLOAD_DIR / f"%(title).80s-%(id)s-{job_id}.%(ext)s"
        )

        base_opts: dict = {
            "outtmpl": output_template,
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragments": 5,
            "quiet": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0 Safari/537.36"
                ),
            },
            "progress_hooks": [lambda d: _job_download_hook(job_id, d)],
        }

        # yt-dlp supports postprocessor hooks; safe to pass even if unused.
        base_opts["postprocessor_hooks"] = [lambda d: _job_postprocessor_hook(job_id, d)]

        if job.file_format == "audio":
            ydl_opts = {
                **base_opts,
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        elif job.file_format == "video_low":
            ydl_opts = {
                **base_opts,
                "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/best[height<=480]/best",
                "merge_output_format": "mp4",
            }
        else:
            ydl_opts = {
                **base_opts,
                "format": "bv*+ba/bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(job.url, download=True)
            if not info_dict:
                raise yt_dlp.utils.DownloadError("No video data returned for this URL")
            filename = ydl.prepare_filename(info_dict)

        file_path = _resolve_final_path(filename)
        if not file_path.exists():
            raise FileNotFoundError("Download finished, but output file could not be found")

        file_path = file_path.resolve()
        download_root = DOWNLOAD_DIR.resolve()
        if download_root not in file_path.parents:
            raise ValueError("Download path outside allowed directory")

        _set_job(
            job_id,
            status="finished",
            stage="Ready",
            filename=str(file_path),
            progress=1.0,
        )
    except Exception as exc:  # noqa: BLE001
        _set_job(job_id, status="error", stage="Error", error=str(exc))


def _cli_progress_hook(status: dict) -> None:
    """
    Simple text progress bar for CLI downloads using yt-dlp progress hooks.
    """
    if status.get("status") == "downloading":
        downloaded = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate")

        if total:
            fraction = max(0.0, min(1.0, downloaded / total))
            bar_len = 40
            filled_len = int(bar_len * fraction)
            bar = "#" * filled_len + "-" * (bar_len - filled_len)
            percent = fraction * 100
            sys.stdout.write(f"\r[{bar}] {percent:5.1f}%")
        else:
            # Fallback when total size is unknown
            mib = downloaded / (1024 * 1024)
            sys.stdout.write(f"\rDownloaded ~{mib:6.2f} MiB")

        sys.stdout.flush()
    elif status.get("status") == "finished":
        sys.stdout.write("\rDownload complete, processing...\n")
        sys.stdout.flush()


def _download_video(url: str, file_format: str, progress: bool = False) -> Path:
    """
    Download a video using yt-dlp and return the path to the downloaded file.
    """
    # Basic safety: don't let yt-dlp write outside our downloads directory
    output_template = str(DOWNLOAD_DIR / "%(title).80s-%(id)s.%(ext)s")

    # Core options that help with HLS-style streams (e.g. Veo, Vimeo) and are safe
    # for other sites as well.
    base_opts: dict = {
        "outtmpl": output_template,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragments": 5,
        # Use a desktop-like User-Agent which can help with some providers.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0 Safari/537.36"
            ),
        },
    }

    # Format selection
    if file_format == "audio":
        ydl_opts = {
            **base_opts,
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    elif file_format == "video_low":
        ydl_opts = {
            **base_opts,
            # Prefer <=480p HLS/MP4 streams when available, fall back to best.
            "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/best[height<=480]/best",
            "merge_output_format": "mp4",
        }
    else:  # "video" or anything else
        ydl_opts = {
            **base_opts,
            # Robust default for mixed sites including Veo/Vimeo/YouTube:
            "format": "bv*+ba/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
        }

    if progress:
        ydl_opts["progress_hooks"] = [_cli_progress_hook]
    else:
        ydl_opts["quiet"] = True

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)
        if not info_dict:
            raise yt_dlp.utils.DownloadError("No video data returned for this URL")
        filename = ydl.prepare_filename(info_dict)

    path = Path(filename)
    # Postprocessors (e.g. FFmpegExtractAudio, merge) can change the final extension
    if not path.exists():
        stem = path.stem
        for ext in (".mp3", ".mp4", ".m4a", ".webm"):
            candidate = path.parent / f"{stem}{ext}"
            if candidate.exists():
                path = candidate
                break
    return path


def _validate_format(file_format: str) -> str:
    if file_format in ("video", "video_low", "audio"):
        return file_format
    return "video"


@app.post("/api/download/start")
def api_download_start(
    url: str = Form(...),
    file_format: str = Form("video"),
):
    url = url.strip()
    if not url:
        return JSONResponse(
            {"error": "Please enter a valid URL."},
            status_code=400,
        )

    file_format = _validate_format(file_format)
    job_id = str(uuid4())

    job = DownloadJob(id=job_id, url=url, file_format=file_format)
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    thread = threading.Thread(target=_run_download_job, args=(job_id,), daemon=True)
    thread.start()

    return {"job_id": job_id}


@app.get("/api/download/progress/{job_id}")
async def api_download_progress(job_id: str):
    async def event_stream():
        last_update = 0.0
        while True:
            job = _get_job(job_id)
            if not job:
                payload = {"status": "error", "stage": "Error", "error": "Unknown job id"}
                yield f"data: {json.dumps(payload)}\n\n"
                return

            if job.updated_at != last_update:
                last_update = job.updated_at
                payload = {
                    "job_id": job.id,
                    "status": job.status,
                    "stage": job.stage,
                    "progress": job.progress,
                    "downloaded_bytes": job.downloaded_bytes,
                    "total_bytes": job.total_bytes,
                    "error": job.error,
                }
                if job.status == "finished":
                    payload["download_url"] = f"/api/download/file/{job.id}"

                yield f"data: {json.dumps(payload)}\n\n"

                if job.status in {"finished", "error"}:
                    return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/download/file/{job_id}")
def api_download_file(job_id: str):
    job = _get_job(job_id)
    if not job:
        return JSONResponse({"error": "Unknown job id"}, status_code=404)
    if job.status != "finished" or not job.filename:
        return JSONResponse({"error": "File is not ready yet"}, status_code=409)

    file_path = Path(job.filename)
    if not file_path.exists():
        return JSONResponse({"error": "File not found on disk"}, status_code=404)

    media_type = "application/octet-stream"
    if file_path.suffix.lower() in {".mp4", ".m4v"}:
        media_type = "video/mp4"
    elif file_path.suffix.lower() in {".webm"}:
        media_type = "video/webm"
    elif file_path.suffix.lower() in {".mp3"}:
        media_type = "audio/mpeg"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
    )


@app.post("/download")
def download(
    request: Request,
    url: str = Form(...),
    file_format: str = Form("video"),
):
    url = url.strip()
    if not url:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Please enter a valid URL.",
                "info": None,
                "url": "",
                "file_format": file_format,
            },
            status_code=400,
        )

    file_format = _validate_format(file_format)

    try:
        file_path = _download_video(url, file_format)
    except yt_dlp.utils.DownloadError as e:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Could not download this URL. Make sure the link is correct and the site allows downloads.",
                "info": str(e),
                "url": url,
                "file_format": file_format,
            },
            status_code=400,
        )
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Unexpected error while downloading the video.",
                "info": str(e),
                "url": url,
                "file_format": file_format,
            },
            status_code=500,
        )

    if not file_path.exists():
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Download finished, but the file could not be found.",
                "info": None,
                "url": url,
                "file_format": file_format,
            },
            status_code=500,
        )

    # Ensure path is under DOWNLOAD_DIR (no symlink / path escape)
    try:
        file_path = file_path.resolve()
        download_root = DOWNLOAD_DIR.resolve()
        if download_root not in file_path.parents:
            raise ValueError("Download path outside allowed directory")
    except (OSError, ValueError):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Download path is invalid.",
                "info": None,
                "url": url,
                "file_format": file_format,
            },
            status_code=500,
        )

    media_type = "application/octet-stream"
    if file_path.suffix.lower() in {".mp4", ".m4v"}:
        media_type = "video/mp4"
    elif file_path.suffix.lower() in {".webm"}:
        media_type = "video/webm"
    elif file_path.suffix.lower() in {".mp3"}:
        media_type = "audio/mpeg"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
    )


# Simple health check for quick verification
@app.get("/health")
def health():
    return {"status": "ok"}


def main() -> None:
    """
    Small CLI wrapper:

    - `launch`  → start the FastAPI web server.
    - `download` → download a single video from the command line with a progress bar.
    """

    parser = argparse.ArgumentParser(description="Video Downloader web app / CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # launch: start the FastAPI server
    launch_parser = subparsers.add_parser(
        "launch", help="Start the web server (FastAPI + uvicorn)"
    )
    launch_parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)"
    )
    launch_parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind (default: 8000)"
    )

    # download: use the same logic as the web route, but via CLI
    download_parser = subparsers.add_parser(
        "download", help="Download a video or audio file from the CLI"
    )
    download_parser.add_argument("url", help="Video URL")
    download_parser.add_argument(
        "--format",
        dest="file_format",
        choices=["video", "video_low", "audio"],
        default="video",
        help="Download format: video (HD, default), video_low (≤480p), audio (MP3)",
    )

    args = parser.parse_args()

    if args.command == "launch":
        import uvicorn

        uvicorn.run("main:app", host=args.host, port=args.port, reload=True)
    elif args.command == "download":
        try:
            file_path = _download_video(args.url.strip(), args.file_format, progress=True)
        except yt_dlp.utils.DownloadError as exc:
            print(f"Download failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
        except Exception as exc:  # noqa: BLE001
            print(f"Unexpected error: {exc}", file=sys.stderr)
            raise SystemExit(1)

        if not file_path.exists():
            print(
                "Download reported success, but the file could not be found on disk.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        print(f"\nSaved to: {file_path}")


if __name__ == "__main__":
    main()


