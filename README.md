# Flask + Selenium-Wire Batch HLS Sniffer with FFmpeg

Web UI to choose one output folder and submit multiple page URLs.
For each URL, the app captures HLS m3u8, estimates quality, and (optionally) runs FFmpeg.
Shows a table with Title (via HTTP), Status (✅/❌), and expandable error/details.

## Quick start
1) Create a venv (recommended):
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   # macOS/Linux:
   source .venv/bin/activate
   ```
2) Install deps:
   ```bash
   pip install -r requirements.txt
   ```
3) Run the server:
   ```bash
   python app.py
   ```
4) Open http://127.0.0.1:5000

## Notes
- Needs Google Chrome/Chromium installed.
- For Windows, paste e.g. `D:\Downloads\Videos` in the "Output folder" field.
- FFmpeg is optional. If not found, the app creates a `run_ffmpeg.cmd` inside the output folder per URL.
- You can disable auto-run by unchecking "Run FFmpeg".
