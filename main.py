from pathlib import Path
from typing import Optional
import argparse
import os
import sys

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse
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
def index(request: Request, error: Optional[str] = None, info: Optional[str] = None):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "error": error,
            "info": info,
            "adsense_client_id": ADSENSE_CLIENT_ID,
            "adsense_slot_id": ADSENSE_SLOT_ID,
        },
    )


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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info_dict)

    return Path(filename)


@app.post("/download")
def download(
    request: Request,
    url: str = Form(...),
    file_format: str = Form("video"),
):
    if not url.strip():
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

    try:
        file_path = _download_video(url.strip(), file_format)
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

    # Let the browser download the file
    return FileResponse(
        path=file_path,
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


