# Flask + Selenium-Wire Batch HLS Sniffer with FFmpeg

Web UI to choose one output folder and submit multiple page URLs.
For each URL, the app captures HLS m3u8, estimates quality, and (optionally) runs FFmpeg.
Shows a table with Title (via HTTP), Status (✅/❌), and expandable error/details.

## New: Efficient M3U8 Capture with Playwright/CDP

This project now includes **SnifferTV-style m3u8 capture** using Playwright or Selenium CDP:
- ✅ **Much lower memory usage** (~100-200MB vs 500MB+ with selenium-wire)
- ✅ **Faster capture** (direct CDP access, no MITM proxy overhead)
- ✅ **Better reliability** (no proxy interference)
- ✅ **Auto-fallback** to selenium-wire if CDP methods fail

The app automatically tries:
1. **Playwright** (fastest, most efficient) ← Recommended!
2. **Selenium CDP** (fast, using Selenium 4)
3. **selenium-wire** (fallback, heavy but reliable)

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
3) **RECOMMENDED**: Install Playwright for best performance:
   ```bash
   pip install playwright
   playwright install chromium
   ```
4) Run the server:
   ```bash
   python app.py
   ```
5) Open http://127.0.0.1:5000

## Testing the New Capture Methods

Run the test suites to verify everything works:

```bash
# Test with local HTTP server (creates sample HLS manifests)
python test_capture_local_m3u8.py

# Test URL detection and Kaltura patterns
python test_capture_remote_example.py
```

## Architecture: How M3U8 Capture Works

### Pipeline Flow

```
URL Input
    ↓
1. Try yt-dlp (fast, works for YouTube/common sites)
    ↓ (fails)
2. Try Playwright CDP (NEW - fast, low memory)
    ↓ (fails or not installed)
3. Try Selenium CDP (NEW - fast, uses Selenium 4)
    ↓ (fails or not installed)
4. Try selenium-wire (FALLBACK - slow, heavy but reliable)
    ↓
Found m3u8 URLs
    ↓
Analyze variants (resolution, bitrate, encryption)
    ↓
Select best variant
    ↓
Download with FFmpeg
```

### New Modules

- **`m3u8_sniffer_utils.py`**: Helper functions for m3u8 detection, URL cleanup, Kaltura pattern matching
- **`m3u8_capture_playwright.py`**: Playwright-based capture (recommended)
- **`m3u8_capture_selenium_cdp.py`**: Selenium 4 + CDP capture (alternative)

### Integration Points

The new capture methods are automatically used in `app.py`:

```python
# In process_single_url():
# 1. Try CDP-based capture first
found_m3u8, master_manifest_url, _ = try_capture_m3u8_via_cdp(page_url, timeout=20)

# 2. If CDP found nothing, fallback to selenium-wire
if not found_m3u8:
    # Use existing selenium-wire code...
```

## Memory Usage Comparison

| Method | Memory | Speed | Reliability |
|--------|--------|-------|-------------|
| **Playwright CDP** | ~150MB | ⚡⚡⚡ Fast | ⭐⭐⭐ Excellent |
| **Selenium CDP** | ~200MB | ⚡⚡ Fast | ⭐⭐⭐ Excellent |
| **selenium-wire** | ~500MB+ | ⚡ Slow | ⭐⭐ Good |

## Using the Capture Functions Directly

You can use the capture functions in your own scripts:

### Playwright Example

```python
from m3u8_capture_playwright import (
    capture_m3u8_via_playwright,
    PlaywrightBrowserManager
)

# Single URL
results = capture_m3u8_via_playwright("https://example.com/video")
for m3u8 in results:
    print(f"Found: {m3u8.url} (master={m3u8.is_master})")

# Multiple URLs with browser reuse (efficient!)
with PlaywrightBrowserManager() as manager:
    for url in urls:
        results = capture_m3u8_via_playwright(url, browser_manager=manager)
        # Process results...
```

### Selenium CDP Example

```python
from m3u8_capture_selenium_cdp import (
    capture_m3u8_via_selenium_cdp,
    SeleniumCDPBrowserManager
)

# Single URL
results = capture_m3u8_via_selenium_cdp("https://example.com/video")

# Multiple URLs with driver reuse
with SeleniumCDPBrowserManager() as manager:
    for url in urls:
        results = capture_m3u8_via_selenium_cdp(url, driver_manager=manager)
        # Process results...
```

### Helper Utilities Example

```python
from m3u8_sniffer_utils import (
    is_m3u8_url,
    is_kaltura_url,
    extract_kaltura_ids,
    clean_m3u8_url,
    filter_and_prioritize_m3u8s
)

# Detect m3u8 URLs
if is_m3u8_url("https://example.com/video.m3u8"):
    print("This is an m3u8 URL!")

# Kaltura detection
if is_kaltura_url(url):
    entry_id, flavor_id = extract_kaltura_ids(url)
    print(f"Kaltura: entry={entry_id}, flavor={flavor_id}")

# Clean URLs (remove tracking params, decode)
clean_url = clean_m3u8_url(raw_url)

# Prioritize captures (master first, highest bandwidth)
best_m3u8s = filter_and_prioritize_m3u8s(captured_m3u8s)
```

## Notes
- Needs Google Chrome/Chromium installed.
- For Windows, paste e.g. `D:\Downloads\Videos` in the "Output folder" field.
- FFmpeg is optional. If not found, the app creates a `run_ffmpeg.cmd` inside the output folder per URL.
- You can disable auto-run by unchecking "Run FFmpeg".
- **Playwright is highly recommended** for 3-5x better memory efficiency and speed.

## Troubleshooting

### "Playwright not available" warning
Install Playwright:
```bash
pip install playwright
playwright install chromium
```

### "No m3u8 URLs found"
- Some sites use DRM or require authentication
- Try waiting longer (increase timeout)
- Check if the site requires user interaction beyond clicking play

### High memory usage
- Make sure Playwright is installed and being used (check logs)
- The app will show "🎯 Attempting m3u8 capture via Playwright (CDP)..." if it's working
- If you see "🌐 Creating browser (selenium-wire fallback)...", Playwright isn't working

## Contributing

Pull requests welcome! Please test with both test suites before submitting:
```bash
python test_capture_local_m3u8.py
python test_capture_remote_example.py
```
