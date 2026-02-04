from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

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
        },
    )


def _download_video(url: str, file_format: str) -> Path:
    """
    Download a video using yt-dlp and return the path to the downloaded file.
    """
    # Basic safety: don't let yt-dlp write outside our downloads directory
    output_template = str(DOWNLOAD_DIR / "%(title).80s-%(id)s.%(ext)s")

    # Format selection
    if file_format == "audio":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
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
            "format": "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "outtmpl": output_template,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }
    else:  # "video" or anything else
        ydl_opts = {
            "format": "bv*+ba/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


