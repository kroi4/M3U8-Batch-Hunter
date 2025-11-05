# Quick Start Guide - New M3U8 Capture

## TL;DR

```bash
# Install Playwright (highly recommended!)
pip install playwright
playwright install chromium

# Run the app
python app.py

# That's it! The app now uses efficient CDP capture automatically.
```

## What Changed?

Your app now has **3 capture methods** instead of 1:

| Method | Speed | Memory | Status |
|--------|-------|--------|--------|
| **Playwright** ⭐ | ⚡⚡⚡ | 150MB | **NEW - Auto-enabled** |
| **Selenium CDP** | ⚡⚡ | 200MB | **NEW - Auto-fallback** |
| **selenium-wire** | ⚡ | 500MB+ | Old fallback |

The app automatically tries them in order until one works.

## Installation

### 1. Update Dependencies

```bash
pip install -r requirements.txt
```

### 2. Install Playwright (STRONGLY Recommended)

```bash
pip install playwright
playwright install chromium
```

This gives you **3-5x better performance** and **3x lower memory usage**.

## Verify It's Working

Run the app and check the logs:

```bash
python app.py
```

When processing a URL, you should see:

```
🎯 Attempting m3u8 capture via Playwright (CDP)...
✅ Playwright captured 5 m3u8 URLs successfully!
```

If you see this, **you're good!** 🎉

If you see:

```
🌐 Creating browser (selenium-wire fallback)...
```

Then Playwright isn't installed or didn't find m3u8s.

## Run Tests

Verify everything works:

```bash
# Test with local sample files
python test_capture_local_m3u8.py

# Test URL detection and Kaltura patterns
python test_capture_remote_example.py
```

Expected output:
```
✅ TEST PASSED: Master manifest detected!
🎉 All tests PASSED!
```

## Using in Your Own Scripts

### Example 1: Single URL

```python
from m3u8_capture_playwright import capture_m3u8_via_playwright

results = capture_m3u8_via_playwright("https://example.com/video")

for m3u8 in results:
    print(f"Found: {m3u8.url}")
    if m3u8.is_master:
        print("  ^ This is the master manifest!")
```

### Example 2: Multiple URLs (Efficient)

```python
from m3u8_capture_playwright import (
    capture_m3u8_via_playwright,
    PlaywrightBrowserManager
)

urls = [
    "https://example.com/video1",
    "https://example.com/video2",
    "https://example.com/video3",
]

# Reuse browser = much faster!
with PlaywrightBrowserManager() as manager:
    for url in urls:
        results = capture_m3u8_via_playwright(
            url, 
            browser_manager=manager,
            timeout=20
        )
        print(f"{url}: Found {len(results)} m3u8s")
```

### Example 3: Detect Kaltura URLs

```python
from m3u8_sniffer_utils import (
    is_kaltura_url,
    extract_kaltura_ids,
    is_kaltura_master_manifest
)

url = "https://cdnapisec.kaltura.com/p/1234/.../entryId/1_abc123/..."

if is_kaltura_url(url):
    print("This is a Kaltura URL!")
    
    if is_kaltura_master_manifest(url):
        print("This is the master manifest!")
    
    entry_id, flavor_id = extract_kaltura_ids(url)
    print(f"Entry: {entry_id}, Flavor: {flavor_id}")
```

## Integration with Existing Code

### Before (selenium-wire only)

```python
# Old code
from seleniumwire import webdriver
driver = webdriver.Chrome()
driver.get(url)
# ... wait and poll driver.requests ...
```

### After (automatic CDP with fallback)

```python
# New code - handles everything automatically!
from m3u8_capture_playwright import capture_m3u8_via_playwright

results = capture_m3u8_via_playwright(url, timeout=20)
# Returns list of M3U8Info objects
```

Or use the convenience wrapper:

```python
from m3u8_capture_playwright import capture_m3u8_via_cdp

results = capture_m3u8_via_cdp(url, timeout=20)
# Returns list of dicts with metadata
```

## Common Use Cases

### Case 1: Find Master Manifest

```python
from m3u8_capture_playwright import capture_m3u8_via_playwright

results = capture_m3u8_via_playwright(url)

# Find master
master = next((m for m in results if m.is_master), None)

if master:
    print(f"Master: {master.url}")
else:
    print("No master found, using first URL")
    print(f"URL: {results[0].url}")
```

### Case 2: Get Highest Quality

```python
from m3u8_sniffer_utils import select_best_variant

results = capture_m3u8_via_playwright(url)
best = select_best_variant(results)

print(f"Best quality: {best.url}")
print(f"Resolution: {best.resolution}")
print(f"Bandwidth: {best.bandwidth}")
```

### Case 3: Clean URLs

```python
from m3u8_sniffer_utils import clean_m3u8_url

messy_url = "https://example.com/video.m3u8?utm_source=spam&fbclid=123&token=abc"

clean_url = clean_m3u8_url(messy_url)
# Result: "https://example.com/video.m3u8?token=abc"
# (removes tracking params, keeps functional ones)
```

## Troubleshooting

### ❌ "Playwright not available"

**Fix:**
```bash
pip install playwright
playwright install chromium
```

### ❌ "No m3u8 URLs found"

**Possible causes:**
1. Site uses DRM (can't capture)
2. Needs authentication/cookies
3. Timeout too short
4. Site has unusual video loading

**Try:**
- Increase timeout: `capture_m3u8_via_playwright(url, timeout=30)`
- Check if site needs login
- Try manually in browser first

### ❌ High memory usage

**Check:**
- Is Playwright installed? (should use ~150MB per capture)
- Are you reusing browser? (should for multiple URLs)
- Is selenium-wire fallback being used? (check logs)

**Fix:**
```python
# Reuse browser for multiple URLs
with PlaywrightBrowserManager() as manager:
    for url in urls:
        capture_m3u8_via_playwright(url, browser_manager=manager)
```

### ❌ "Browser process exited"

**Fix:**
```bash
playwright install chromium
```

Make sure Chromium is installed for Playwright.

## Performance Tips

### ✅ DO: Reuse Browser Instance

```python
# GOOD - Reuse browser
with PlaywrightBrowserManager() as manager:
    for url in urls:
        capture_m3u8_via_playwright(url, browser_manager=manager)
```

```python
# BAD - New browser each time
for url in urls:
    capture_m3u8_via_playwright(url)  # Creates new browser!
```

### ✅ DO: Use Appropriate Timeouts

```python
# Fast sites
results = capture_m3u8_via_playwright(url, timeout=10)

# Slow sites
results = capture_m3u8_via_playwright(url, timeout=30)
```

### ✅ DO: Let It Exit Early

The capture functions automatically exit early when they find the master manifest. No need to wait the full timeout!

```python
# Will exit as soon as master manifest is found (usually 1-3 seconds)
results = capture_m3u8_via_playwright(url, timeout=20)
```

## API Reference

### Main Functions

```python
# Playwright capture (recommended)
capture_m3u8_via_playwright(url, browser_manager=None, timeout=20, wait_after_load=3.0)

# Selenium CDP capture
capture_m3u8_via_selenium_cdp(url, driver_manager=None, timeout=20, wait_after_load=3.0)

# Convenience wrapper
capture_m3u8_via_cdp(url, browser_or_driver=None, timeout=20)
```

### Helper Functions

```python
# Detection
is_m3u8_url(url) -> bool
is_hls_content_type(content_type) -> bool
is_kaltura_url(url) -> bool
is_kaltura_master_manifest(url) -> bool

# Extraction
extract_kaltura_ids(url) -> (entry_id, flavor_id)
clean_m3u8_url(url) -> str
strip_tracking_params(url) -> str

# Prioritization
filter_and_prioritize_m3u8s(m3u8_list) -> List[M3U8Info]
select_best_variant(m3u8_list) -> M3U8Info
```

## What's Next?

1. ✅ Install Playwright for best performance
2. ✅ Run tests to verify everything works
3. ✅ Check logs to see which method is being used
4. ✅ Monitor memory usage (should be much lower!)
5. ✅ Enjoy faster, more efficient m3u8 capture! 🎉

## Need More Help?

- **Full documentation**: See `README.md`
- **Implementation details**: See `IMPLEMENTATION_SUMMARY.md`
- **Run tests**: `python test_capture_local_m3u8.py`
- **Check logs**: Look for "🎯" emoji in console output

---

**Key Takeaway**: Just install Playwright and everything gets 3-5x faster with 3x less memory. The app handles all the complexity automatically! 🚀

