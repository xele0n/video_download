## Video Downloader Web App

This is a small FastAPI-based web application to download videos (or audio) from many popular video platforms by pasting a link (for example, YouTube, Vimeo and others supported by `yt-dlp`).

**Important:** Only use this app to download content that you own or have explicit permission to download. Respect copyrights and the terms of service of each website.

### Features

- **Web UI**: Clean, modern single-page interface with Tailwind CSS.
- **Multiple formats**:
  - HD video (MP4)
  - Smaller video (≤ 480p)
  - Audio-only (MP3)
- **Error handling**: Friendly error messages if a download fails.

### Requirements

- Python 3.10+ (recommended)
- FFmpeg installed and available in your PATH (needed for some conversions, e.g. MP3).

On Windows you can install FFmpeg, for example, via:

- Downloading a static build from the official FFmpeg website and adding the `bin` folder to your PATH.

### Setup

1. Create and activate a virtual environment (recommended):

   ```bash
   cd video_download
   python -m venv venv
   venv\Scripts\activate  # On Windows PowerShell
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the development server:

   ```bash
   uvicorn main:app --reload
   ```

4. Open your browser and go to:

   ```text
   http://127.0.0.1:8000
   ```

### How it works

- The frontend (in `templates/index.html`) provides a form where you paste a video URL and select the format.
- The backend (`main.py`) uses `yt-dlp` to download the video/audio into the local `downloads/` folder and immediately returns the file to the browser as a download.

### Notes

- Some platforms may block or limit downloads or change their APIs; when that happens, `yt-dlp` might need to be updated:

  ```bash
  pip install --upgrade yt-dlp
  ```

- If a download fails, check that:
  - The URL is correct and publicly accessible.
  - The platform is supported by `yt-dlp`.
  - The content is allowed to be downloaded according to the site's terms.


