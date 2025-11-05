from flask import Flask, render_template, request, jsonify
import os, time, re, requests, shutil, subprocess, unicodedata, html as html_mod
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Optional, List, Tuple

from seleniumwire import webdriver  # pip install selenium-wire
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from yt_dlp import YoutubeDL

# Import new m3u8 capture modules
try:
    from m3u8_capture_playwright import (
        capture_m3u8_via_playwright,
        PlaywrightBrowserManager,
        PLAYWRIGHT_AVAILABLE
    )
    PLAYWRIGHT_ENABLED = PLAYWRIGHT_AVAILABLE
except ImportError:
    PLAYWRIGHT_ENABLED = False
    logger = logging.getLogger(__name__)
    logger.warning("Playwright not available. Install with: pip install playwright && playwright install chromium")

try:
    from m3u8_capture_selenium_cdp import (
        capture_m3u8_via_selenium_cdp,
        SeleniumCDPBrowserManager
    )
    SELENIUM_CDP_ENABLED = True
except ImportError:
    SELENIUM_CDP_ENABLED = False

from m3u8_sniffer_utils import filter_and_prioritize_m3u8s, select_best_variant

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)

UA = "Mozilla/5.0"
DEFAULT_REFERER = None

# ---------------- Generic downloader (yt-dlp) ----------------
def _format_progress_percent(d: dict) -> float | None:
    total = d.get('total_bytes') or d.get('total_bytes_estimate') or None
    downloaded = d.get('downloaded_bytes') or None
    if total and downloaded:
        try:
            return max(0.0, min(100.0, (downloaded / float(total)) * 100.0))
        except Exception:
            return None
    return None

def download_with_ytdlp(page_url: str, out_dir: str, progress_cb=None) -> dict:
    """
    Generic extractor/downloader using yt-dlp.
    Emits progress via progress_cb(dict) where dict contains keys: status, percent, speed, eta, filename
    Returns: dict with fields: status, title, output, details
    Raises: Exception if extraction/download fails
    """
    os.makedirs(out_dir, exist_ok=True)

    print(f"[yt-dlp] מנסה להוריד: {page_url}")

    def _hook(d):
        try:
            kind = d.get('status')
            if kind in ("downloading", "finished"):
                pct = _format_progress_percent(d)
                if kind == "finished":
                    pct = 100.0
                msg = {
                    "status": kind,
                    "percent": pct,
                    "speed": d.get('speed'),
                    "eta": d.get('eta'),
                    "filename": d.get('filename')
                }
                if progress_cb:
                    progress_cb(msg)
        except Exception:
            pass

    ydl_opts = {
        'quiet': False,  # Changed to False for debugging
        'verbose': True,  # Enable verbose logging
        'noprogress': True,
        'outtmpl': os.path.join(out_dir, '%(title)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'concurrent_fragment_downloads': 4,
        'retries': 3,
        'http_headers': {
            'User-Agent': UA,
            'Accept-Language': 'he-IL,he;q=0.9,en-US;q=0.8',
            'Referer': page_url,
        },
        'progress_hooks': [_hook],
        # Prefer best video+audio; fall back to best muxed
        'format': 'bv*+ba/b',
    }

    with YoutubeDL(ydl_opts) as ydl:
        print(f"[yt-dlp] מחלץ מידע...")
        info = ydl.extract_info(page_url, download=True)
        print(f"[yt-dlp] הורדה הושלמה: {info.get('title')}")
        
        # Compute output path
        ext = info.get('ext') or 'mp4'
        title = info.get('title') or 'video'
        out_path = os.path.join(out_dir, f"{safe_windows_filename(title)}.{ext}")
        # yt-dlp may write into slightly different filename; try to find actual file
        if not os.path.exists(out_path):
            # Try from ydl prepare_filename
            try:
                tentative = ydl.prepare_filename(info)
                if os.path.exists(tentative):
                    out_path = tentative
            except Exception:
                pass
        return {
            "status": "ok",
            "title": title,
            "output": out_path,
            "details": f"✅ הורד באמצעות yt-dlp",
        }

# ---------------- Title helpers ----------------
def _norm_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\u200B-\u200F\u202A-\u202E]", "", s)
    s = s.strip().strip('\'"“”„‟‚‛❝❞')
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_title_via_http(url: str, timeout=12) -> Optional[str]:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": UA, "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8", "Referer": url},
            timeout=timeout,
        )
        r.raise_for_status()
        html_text = r.text
    except Exception:
        return None

    m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html_text, re.I | re.S)
    if m:
        t = _norm_text(html_mod.unescape(m.group(1)))
        if t:
            return t

    m = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.I | re.S)
    if m:
        inner = re.sub(r"<[^>]+>", " ", m.group(1))
        t = _norm_text(html_mod.unescape(inner))
        if t:
            return t
    return None

def safe_windows_filename(name: str, max_len: int = 150) -> str:
    if not name:
        return "video"
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r'[<>:\"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    reserved = {"CON","PRN","AUX","NUL", *{f"COM{i}" for i in range(1,10)}, *{f"LPT{i}" for i in range(1,10)}}
    base_only = os.path.splitext(name)[0].upper()
    if base_only in reserved:
        name = f"_{name}"
    name = name.rstrip(" .")
    return (name[:max_len].rstrip()) or "video"

# ---------------- New M3U8 Capture (CDP-based) ----------------
def try_capture_m3u8_via_cdp(page_url: str, timeout: int = 20) -> Tuple[set, Optional[str], dict]:
    """
    Try to capture m3u8 URLs using new CDP-based methods (Playwright or Selenium CDP).
    
    This is much lighter and faster than selenium-wire.
    
    Args:
        page_url: URL to capture from
        timeout: Timeout in seconds
        
    Returns:
        Tuple of (found_m3u8_urls_set, master_manifest_url, empty_dict_for_headers)
        Returns (set(), None, {}) if capture fails or no URLs found
    """
    logger = logging.getLogger(__name__)
    
    # Try Playwright first (most efficient)
    if PLAYWRIGHT_ENABLED:
        logger.info("🎯 Attempting m3u8 capture via Playwright (CDP)...")
        try:
            m3u8_infos = capture_m3u8_via_playwright(
                url=page_url,
                browser_manager=None,  # Fresh browser for now
                timeout=timeout,
                wait_after_load=3.0
            )
            
            if m3u8_infos:
                logger.info(f"✅ Playwright captured {len(m3u8_infos)} m3u8 URLs")
                
                # Convert to set of URLs
                found_urls = {info.url for info in m3u8_infos}
                
                # Find master manifest
                master_url = None
                for info in m3u8_infos:
                    if info.is_master:
                        master_url = info.url
                        logger.info(f"🎯 Master manifest found: {master_url[:80]}...")
                        break
                
                return found_urls, master_url, {}
            else:
                logger.warning("⚠️ Playwright found no m3u8 URLs")
        except Exception as e:
            logger.warning(f"⚠️ Playwright capture failed: {e}")
    
    # Try Selenium CDP as fallback
    if SELENIUM_CDP_ENABLED:
        logger.info("🎯 Attempting m3u8 capture via Selenium CDP...")
        try:
            m3u8_infos = capture_m3u8_via_selenium_cdp(
                url=page_url,
                driver_manager=None,  # Fresh driver for now
                timeout=timeout,
                wait_after_load=3.0
            )
            
            if m3u8_infos:
                logger.info(f"✅ Selenium CDP captured {len(m3u8_infos)} m3u8 URLs")
                
                # Convert to set of URLs
                found_urls = {info.url for info in m3u8_infos}
                
                # Find master manifest
                master_url = None
                for info in m3u8_infos:
                    if info.is_master:
                        master_url = info.url
                        logger.info(f"🎯 Master manifest found: {master_url[:80]}...")
                        break
                
                return found_urls, master_url, {}
            else:
                logger.warning("⚠️ Selenium CDP found no m3u8 URLs")
        except Exception as e:
            logger.warning(f"⚠️ Selenium CDP capture failed: {e}")
    
    # If both methods unavailable or failed
    logger.info("ℹ️ CDP capture not available or failed, will use selenium-wire fallback")
    return set(), None, {}

# ---------------- HLS helpers ----------------
def parse_attribute_list(s: str):
    out = {}
    for part in re.split(r',(?![^\"]*\")', s):
        if '=' in part:
            k, v = part.split('=', 1)
            v = v.strip()
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            out[k.strip()] = v
    return out

def fetch_text(url: str, referer: str):
    r = requests.get(url, headers={"Referer": referer or url, "User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return r.text

def head_size(url: str, referer: str):
    try:
        h = requests.head(url, headers={"Referer": referer or url, "User-Agent": UA}, timeout=15, allow_redirects=True)
        if h.status_code < 400:
            cl = h.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else None
    except Exception:
        pass
    return None

def list_variants_from_master(master_url: str, referer: str):
    text = fetch_text(master_url, referer=referer)
    variants = []
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if line.upper().startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            if i + 1 < len(lines):
                pl = urljoin(master_url, lines[i + 1].strip())
                bw = attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH") or "0"
                try: bw = int(bw)
                except: bw = 0
                variants.append({
                    "playlist": pl,
                    "bandwidth": bw,
                    "resolution": attrs.get("RESOLUTION"),
                    "codecs": attrs.get("CODECS")
                })
    return variants

def analyze_variant(variant_url: str, referer: str):
    text = fetch_text(variant_url, referer=referer)
    lines = [ln.strip() for ln in text.splitlines()]
    encrypted = any(ln.startswith("#EXT-X-KEY:") for ln in lines)
    is_vod = any(ln == "#EXT-X-ENDLIST" for ln in lines)

    total_seconds = 0.0
    segments = []
    last_dur = None
    for ln in lines:
        if ln.startswith("#EXTINF:"):
            dur_str = ln.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                last_dur = float(dur_str)
            except:
                last_dur = None
        elif not ln.startswith("#") and ln:
            seg_url = urljoin(variant_url, ln)
            if last_dur is not None:
                segments.append((last_dur, seg_url))
                total_seconds += last_dur
                last_dur = None

    return {
        "is_vod": is_vod,
        "total_seconds": total_seconds if is_vod else None,
        "encrypted": encrypted,
        "segments": segments
    }

def human_time(sec: float | int | None):
    if sec is None:
        return "N/A"
    s = int(round(sec))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def estimate_bitrate_mbps(segments, referer: str, sample_n=3):
    samples = segments[:sample_n]
    if not samples: return None
    total_bytes = 0
    total_secs = 0.0
    for dur, surl in samples:
        size = head_size(surl, referer=referer)
        if size:
            total_bytes += size
            total_secs += dur
    if total_secs == 0 or total_bytes == 0:
        return None
    return (total_bytes * 8 / total_secs) / 1e6  # Mbps

def guess_resolution_from_bitrate(mbps: float | None) -> str | None:
    if mbps is None:
        return None
    if mbps >= 4.5:   return "1920x1080"
    if mbps >= 2.5:   return "1280x720"
    if mbps >= 1.2:   return "854x480"
    if mbps >= 0.8:   return "640x360"
    if mbps >= 0.45:  return "426x240"
    return "256x144"

def extract_ids(url: str):
    m = re.search(r"/entryId/([^/]+)/.*?/flavorId/([^/]+)/", url)
    return (m.group(1), m.group(2)) if m else (None, None)

# ---------------- Selenium-wire helpers ----------------
PLAY_BTN_XPATH = '//*[@id="player-gui"]/div[3]/div[1]/div[3]/button'
PLAY_BTN_CSS   = "button.playkit-pre-playback-play-button"

def try_click_el(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", el)
        time.sleep(0.05)
        try:
            el.click(); return True
        except Exception:
            pass
        try:
            ActionChains(driver).move_to_element(el).pause(0.05).click().perform(); return True
        except Exception:
            pass
        try:
            driver.execute_script("arguments[0].click();", el); return True
        except Exception:
            pass
    except StaleElementReferenceException:
        pass
    except Exception:
        pass
    return False

def click_once_everywhere(driver):
    did = False
    try:
        for el in driver.find_elements(By.XPATH, PLAY_BTN_XPATH):
            if el.is_displayed():
                did |= try_click_el(driver, el)
    except Exception:
        pass
    try:
        for sel in [PLAY_BTN_CSS, ".vjs-big-play-button", 'button[title="Play"]',
                    'button[aria-label^="נגן"]', 'button[aria-label^="Play"]']:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    did |= try_click_el(driver, el)
    except Exception:
        pass
    try:
        driver.execute_script("""
            (function(){
              var vids = document.getElementsByTagName('video');
              for (var v of vids) { try { v.muted = true; v.play(); } catch(e){} }
            })();
        """)
    except Exception:
        pass
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except Exception:
        frames = []
    for fr in frames:
        try:
            driver.switch_to.frame(fr)
            try:
                for el in driver.find_elements(By.XPATH, PLAY_BTN_XPATH):
                    if el.is_displayed():
                        did |= try_click_el(driver, el)
            except Exception:
                pass
            try:
                for sel in [PLAY_BTN_CSS, ".vjs-big-play-button", 'button[title="Play"]',
                            'button[aria-label^="נגן"]', 'button[aria-label^="Play"]']:
                    for el in driver.find_elements(By.CSS_SELECTOR, sel):
                        if el.is_displayed():
                            did |= try_click_el(driver, el)
            except Exception:
                pass
            try:
                driver.execute_script("""
                    (function(){
                      var vids = document.getElementsByTagName('video');
                      for (var v of vids) { try { v.muted = true; v.play(); } catch(e){} }
                    })();
                """)
            except Exception:
                pass
        except Exception:
            pass
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return did

def poll_m3u8_optimized(driver, max_seconds=30):
    """
    Optimized version using wait_for_request for immediate capture.
    This is MUCH faster than polling!
    """
    import threading
    
    found = set()
    master_request = None
    stop_clicking = threading.Event()
    
    def click_continuously():
        """Background thread to keep clicking play button"""
        while not stop_clicking.is_set():
            try:
                click_once_everywhere(driver)
                time.sleep(0.3)
            except Exception:
                pass
    
    # Start clicking in background
    click_thread = threading.Thread(target=click_continuously, daemon=True)
    click_thread.start()
    
    try:
        # Wait specifically for master manifest - this is BLOCKING and fast!
        print(f"⏳ ממתין ל-Master Manifest (timeout: {max_seconds}s)...")
        
        try:
            # Wait for master manifest specifically
            request = driver.wait_for_request(
                '.*kaltura.com.*playmanifest.*m3u8', 
                timeout=max_seconds
            )
            print(f"✅ נתפס Master Manifest מיד!")
            master_request = request
            found.add(request.url)
            stop_clicking.set()  # Stop clicking immediately
            
        except Exception as e:
            print(f"⚠️ לא נתפס Master, מחפש m3u8 כלליים... ({e})")
            # Fallback: collect any m3u8 files
            try:
                request = driver.wait_for_request('.*\\.m3u8', timeout=10)
                found.add(request.url)
                print(f"✅ נמצא m3u8 חלופי")
            except Exception:
                pass
            stop_clicking.set()
        
        # Small grace period to catch additional variants
        time.sleep(0.5)
        
        # Collect all m3u8 requests found so far
        for req in driver.requests:
            if not getattr(req, "response", None):
                continue
            url = req.url or ""
            ct = (req.response.headers or {}).get("Content-Type", "").lower()
            if (".m3u8" in url.lower()) or ("mpegurl" in ct):
                found.add(url)
                # Check for master manifest from any Kaltura CDN
                if "kaltura.com" in url and "playmanifest" in url.lower():
                    master_request = req
                    
    finally:
        stop_clicking.set()
        click_thread.join(timeout=1)
    
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in driver.get_cookies()])
    
    master_manifest_url = None
    for u in found:
        # Look for master manifest from any Kaltura CDN
        if "kaltura.com" in u and "playmanifest" in u.lower():
            master_manifest_url = u
            break
    
    captured_headers = {}
    if master_request:
        try:
            for k, v in (master_request.headers or {}).items():
                captured_headers[k] = v
        except Exception:
            pass
    
    return found, master_manifest_url, captured_headers, cookie_str

def poll_m3u8(driver, max_seconds=90, interval=0.3, grace_after_first=2, min_links_to_stop=2):
    """Fallback polling method (kept for compatibility)"""
    start = time.time()
    found = set()
    t_first = None
    master_request = None
    master_found_time = None

    while time.time() - start < max_seconds:
        # Only try clicking if we haven't found master yet
        if master_found_time is None:
            click_once_everywhere(driver)
        
        try:
            for req in driver.requests:
                if not getattr(req, "response", None):
                    continue
                url = req.url or ""
                ct  = (req.response.headers or {}).get("Content-Type", "").lower()
                if (".m3u8" in url.lower()) or ("mpegurl" in ct):
                    found.add(url)
                    # Check if this is the master manifest (any Kaltura CDN)
                    if "kaltura.com" in url and "playmanifest" in url.lower():
                        master_request = req
                        if master_found_time is None:
                            master_found_time = time.time()
                            print(f"✅ נתפס Master Manifest! ממתין {grace_after_first} שניות נוספות...")
        except Exception:
            pass

        has_master = any(("kaltura.com" in u and "playmanifest" in u.lower()) for u in found)
        
        # If we found master manifest, wait only grace_after_first seconds and stop
        if has_master and master_found_time and (time.time() - master_found_time >= grace_after_first):
            print(f"⚡ Master Manifest נתפס, סוגר דפדפן!")
            break
        
        # Original logic for non-master m3u8 files
        if found and t_first is None:
            t_first = time.time()
        if t_first is not None and len(found) >= min_links_to_stop and (time.time() - t_first >= grace_after_first):
            break

        # Shorter sleep when master is found (just waiting for grace period)
        if master_found_time:
            time.sleep(0.1)  # Fast polling while waiting for grace period
        else:
            time.sleep(interval)

    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in driver.get_cookies()])

    master_manifest_url = None
    for u in found:
        # Look for master manifest from any Kaltura CDN
        if "kaltura.com" in u and "playmanifest" in u.lower():
            master_manifest_url = u
            break

    captured_headers = {}
    if master_request:
        try:
            for k, v in (master_request.headers or {}).items():
                captured_headers[k] = v
        except Exception:
            pass

    return found, master_manifest_url, captured_headers, cookie_str

# ---------------- FFmpeg helpers ----------------
def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "video"

def build_ffmpeg_cmd(m3u8_url: str, out_mp4: str, referer: str, ua: str = UA) -> str:
    headers = f"Referer: {referer}\\r\\nUser-Agent: {ua}"
    cmd = (
        'ffmpeg -y '
        '-loglevel warning -stats '
        '-protocol_whitelist "file,http,https,tcp,tls,crypto" '
        f'-headers "{headers}" '
        f'-i "{m3u8_url}" '
        '-c copy -movflags +faststart -bsf:a aac_adtstoasc '
        f'"{out_mp4}"'
    )
    return cmd

def parse_ffmpeg_progress(line: str) -> dict:
    """Parse FFmpeg progress line to extract frame, fps, time, bitrate, speed"""
    result = {}
    try:
        # FFmpeg outputs like: frame=12345 fps=30 q=-1.0 size=1234kB time=00:12:34.56 bitrate=1234.5kbits/s speed=1.5x
        if 'frame=' in line:
            match = re.search(r'frame=\s*(\d+)', line)
            if match:
                result['frame'] = int(match.group(1))
        
        if 'fps=' in line:
            match = re.search(r'fps=\s*([\d.]+)', line)
            if match:
                result['fps'] = float(match.group(1))
        
        if 'time=' in line:
            match = re.search(r'time=\s*([\d:\.]+)', line)
            if match:
                result['time'] = match.group(1)
        
        if 'bitrate=' in line:
            match = re.search(r'bitrate=\s*([\d.]+)\s*kbits/s', line)
            if match:
                result['bitrate'] = f"{match.group(1)} kbps"
        
        if 'speed=' in line:
            match = re.search(r'speed=\s*([\d.]+)x', line)
            if match:
                result['speed'] = f"{match.group(1)}x"
        
        if 'size=' in line:
            match = re.search(r'size=\s*(\d+)kB', line)
            if match:
                result['size'] = f"{match.group(1)} KB"
    except Exception:
        pass
    return result

def run_ffmpeg_with_progress(cmd: str, progress_callback=None) -> int:
    """
    Run FFmpeg command and capture real-time progress.
    Calls progress_callback(dict) with frame, fps, time, bitrate, speed info.
    Returns the exit code.
    """
    import subprocess
    
    try:
        # Run FFmpeg with pipes to capture stderr (where stats are printed)
        process = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding='utf-8',
            errors='replace'
        )
        
        # Read output line by line
        for line in process.stdout:
            line = line.strip()
            if line:
                # Parse progress info
                progress_info = parse_ffmpeg_progress(line)
                if progress_info and progress_callback:
                    progress_callback(progress_info)
        
        # Wait for process to complete
        process.wait()
        return process.returncode
        
    except Exception as e:
        print(f"FFmpeg error: {e}")
        return 1

def ensure_unique_path(path: str) -> str:
    base, ext = os.path.splitext(path)
    n = 1
    final = path
    while os.path.exists(final):
        final = f"{base}_{n}{ext}"
        n += 1
    return final

# ---------------- Core per-URL processing ----------------
def process_single_url(page_url: str, out_dir: str, run_now: bool = True, progress_callback=None):
    """
    Process a single URL to extract and download HLS video.
    Creates a fresh Chrome driver for each URL to avoid session issues.
    
    Args:
        page_url: URL of the page to process
        out_dir: Output directory for downloaded files
        run_now: Whether to run FFmpeg immediately
        progress_callback: Optional callback function for progress updates
    """
    def log_progress(msg):
        """Helper to log progress if callback is provided"""
        print(f"[{page_url}] {msg}")  # Console logging
        if progress_callback:
            progress_callback(msg)
    
    log_progress("📥 מושך כותרת...")
    title_http = extract_title_via_http(page_url)
    title_safe = safe_windows_filename(title_http) if title_http else None

    found_m3u8 = set()
    master_manifest_url = None
    referer = page_url
    
    # ===== NEW: Try CDP-based capture first (Playwright or Selenium CDP) =====
    log_progress("🎯 מנסה לכידה באמצעות CDP (מהיר וקל)...")
    try:
        found_m3u8, master_manifest_url, _ = try_capture_m3u8_via_cdp(page_url, timeout=20)
        
        if found_m3u8:
            log_progress(f"✅ CDP לכד {len(found_m3u8)} קישורי m3u8 בהצלחה!")
        else:
            log_progress("⚠️ CDP לא מצא m3u8, עובר ל-selenium-wire fallback...")
    except Exception as e:
        log_progress(f"⚠️ CDP נכשל: {e}, עובר ל-selenium-wire fallback...")
    
    # ===== Fallback to selenium-wire if CDP didn't find anything =====
    if not found_m3u8:
        log_progress("🌐 יוצר דפדפן (selenium-wire fallback)...")
        options = webdriver.ChromeOptions()
        options.add_argument("--mute-audio")
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--disable-notifications")
        options.add_argument("--window-size=1366,900")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # Use eager page load strategy - don't wait for all resources (images, etc)
        options.page_load_strategy = 'eager'
        # Note: NOT using headless mode as it may interfere with video player detection
        
        # Add logging to help debug
        options.add_experimental_option('excludeSwitches', ['enable-logging'])

        driver = None

        try:
            # Create driver with fresh service each time
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            
            # Set page load timeout to 60 seconds (sometimes pages are slow)
            driver.set_page_load_timeout(60)
            
            log_progress("📄 טוען דף...")
            try:
                driver.get(page_url)
                # Wait for page to be at least interactive (don't need full complete with eager strategy)
                WebDriverWait(driver, 30).until(lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"])
                time.sleep(1.5)
            except TimeoutException:
                log_progress("⚠️ הדף לוקח זמן לטעון, ממשיך בכל זאת...")
                # Continue anyway - the page might be loaded enough
                time.sleep(2)
            
            log_progress("🍪 מקבל עוגיות...")
            # Accept cookies if present
            for sel in ['#onetrust-accept-btn-handler', 'button[aria-label*="מסכים"]', 'button[aria-label*="מאשר"]']:
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    if els:
                        try_click_el(driver, els[0])
                        break
                except Exception:
                    pass

            log_progress("🎬 מחפש וידאו ומנסה להפעיל...")
            # Use optimized version with wait_for_request (MUCH faster!)
            found_m3u8, master_manifest_url, captured_headers, cookie_str = poll_m3u8_optimized(
                driver,
                max_seconds=25  # Should be found within seconds, not minutes!
            )
            
            log_progress(f"✅ נמצאו {len(found_m3u8)} קישורי m3u8")
            
        except Exception as e:
            error_msg = f"Selenium/Load error: {str(e)}"
            return {
                "url": page_url,
                "title": title_http or "(ללא כותרת)",
                "status": "error",
                "emoji": "❌",
                "details": error_msg,
            }
        finally:
            # Always close driver in finally block to ensure cleanup
            if driver:
                try:
                    # Clear requests to free memory
                    if hasattr(driver, 'requests'):
                        try:
                            del driver.requests
                        except Exception:
                            pass
                    # Quit the driver
                    driver.quit()
                    # Small delay to ensure cleanup
                    time.sleep(0.5)
                except Exception as quit_error:
                    print(f"Warning: Failed to quit driver: {quit_error}")

    log_progress("🔍 מנתח וריאנטים...")
    
    # Debug: print all found m3u8s
    log_progress(f"🔎 נמצאו {len(found_m3u8)} קישורי m3u8:")
    for u in found_m3u8:
        log_progress(f"  → {u[:100]}...")  # Print first 100 chars
    
    # Try to find master manifest
    master = None
    for u in found_m3u8:
        # Look for both cdnapisec and cfvod (both are Kaltura CDNs)
        if ("cdnapisec.kaltura.com" in u or "cfvod.kaltura.com" in u) and "playmanifest" in u.lower():
            master = u
            log_progress(f"✅ נמצא Master Manifest")
            break

    # Try to extract variants from master
    variants = list_variants_from_master(master, referer=referer) if master else []
    
    # If no variants from master, try to find variant playlists directly
    if not variants:
        log_progress("🔍 לא נמצאו variants מ-master, מנסה m3u8s ישירים...")
        for u in sorted(found_m3u8):
            # Try all m3u8 files from Kaltura CDN
            if u.lower().endswith(".m3u8") and ("kaltura.com" in u.lower()):
                log_progress(f"  → מנסה: {u[:80]}...")
                variants.append({"playlist": u, "bandwidth": 0, "resolution": None, "codecs": None})

    log_progress(f"📊 מנתח {len(variants)} וריאנטי איכות...")
    rows = []
    dur_by_entry = {}
    for v in variants:
        try:
            a = analyze_variant(v["playlist"], referer=referer)
        except Exception as e:
            log_progress(f"⚠️ שגיאה בניתוח וריאנט {v['playlist'][:80]}: {e}")
            # Skip this variant and try the next one
            continue
        declared_mbps = (v["bandwidth"] / 1e6) if v.get("bandwidth") else None
        est_mbps = declared_mbps or estimate_bitrate_mbps(a["segments"], referer=referer, sample_n=3)
        resolution = v.get("resolution") or guess_resolution_from_bitrate(est_mbps) or "N/A"

        entryId, flavorId = extract_ids(v["playlist"])
        if entryId and a["total_seconds"]:
            dur_by_entry.setdefault(entryId, 0)
            dur_by_entry[entryId] = max(dur_by_entry[entryId], a["total_seconds"])

        rows.append({
            "entryId": entryId,
            "flavorId": flavorId,
            "resolution": resolution,
            "bitrate": (f"{declared_mbps:.2f} Mbps" if declared_mbps is not None else (f"~{est_mbps:.2f} Mbps (אומדן)" if est_mbps else "N/A")),
            "duration_seconds": a["total_seconds"],
            "duration_txt": human_time(a["total_seconds"]),
            "encrypted": "כן" if a["encrypted"] else "לא/לא ידוע",
            "playlist": v["playlist"],
            "sort_mbps": (declared_mbps if declared_mbps is not None else (est_mbps or 0.0))
        })

    if dur_by_entry:
        best_entry = max(dur_by_entry.items(), key=lambda kv: kv[1])[0]
        rows = [r for r in rows if r["entryId"] == best_entry]

    rows.sort(key=lambda r: r["sort_mbps"], reverse=True)

    if not rows:
        log_progress("❌ לא נמצאו וריאנטים תקינים")
        error_details = f"❌ לא נמצאו וריאנטים תקינים להמרה\n\n"
        error_details += f"📊 נמצאו {len(found_m3u8)} קישורי m3u8 אבל לא ניתן לנתח אותם\n"
        if found_m3u8:
            error_details += "\nקישורים שנמצאו:\n"
            for u in list(found_m3u8)[:3]:  # Show first 3
                error_details += f"  • {u[:100]}...\n"
        return {
            "url": page_url,
            "title": title_http or "(ללא כותרת)",
            "status": "error",
            "emoji": "❌",
            "details": error_details
        }

    top = rows[0]
    log_progress(f"🎯 בחר איכות מיטבית: {top.get('resolution', 'N/A')}")
    
    if title_safe:
        base = title_safe
    else:
        base = top.get("entryId") or "video"
        if top.get("flavorId"):
            base += f"_{top['flavorId']}"
        if top.get("resolution") and "N/A" not in top["resolution"]:
            res_clean = re.sub(r"[^\dxp]", "", top["resolution"])
            if res_clean:
                base += f"_{res_clean}"

    out_name = sanitize_filename(base) + ".mp4"
    os.makedirs(out_dir, exist_ok=True)
    out_path = ensure_unique_path(os.path.join(out_dir, out_name))

    ff_cmd = build_ffmpeg_cmd(m3u8_url=top["playlist"], out_mp4=out_path, referer=page_url, ua=UA)
    has_ffmpeg = shutil.which("ffmpeg") is not None

    # Optionally run ffmpeg now
    ran = False
    retcode = None
    if run_now and has_ffmpeg:
        log_progress("🎬 מריץ FFmpeg להורדה...")
        
        # Create a callback that formats FFmpeg progress for the UI
        def ffmpeg_progress_cb(info):
            """Format FFmpeg progress info and send to UI"""
            msg_parts = []
            if 'time' in info:
                msg_parts.append(f"⏱️ {info['time']}")
            if 'speed' in info:
                msg_parts.append(f"⚡ {info['speed']}")
            if 'fps' in info:
                msg_parts.append(f"🎞️ {info['fps']} fps")
            if 'bitrate' in info:
                msg_parts.append(f"📊 {info['bitrate']}")
            
            if msg_parts:
                status_msg = " | ".join(msg_parts)
                log_progress(f"⬇️ {status_msg}")
        
        try:
            retcode = run_ffmpeg_with_progress(ff_cmd, progress_callback=ffmpeg_progress_cb)
            ran = True
            log_progress(f"✅ FFmpeg הסתיים עם קוד {retcode}")
        except Exception as e:
            log_progress(f"❌ FFmpeg נכשל: {e}")
            return {
                "url": page_url,
                "title": title_http or "(ללא כותרת)",
                "status": "error",
                "emoji": "❌",
                "details": f"❌ שגיאת FFmpeg: {str(e)}",
                "ffmpeg_cmd": ff_cmd,
                "output": out_path
            }

    if run_now and has_ffmpeg and ran and retcode == 0 and os.path.exists(out_path):
        # Calculate file size
        file_size_mb = os.path.getsize(out_path) / (1024 * 1024)
        return {
            "url": page_url,
            "title": title_http or "(ללא כותרת)",
            "status": "ok",
            "emoji": "✅",
            "details": f"✅ הורדה הושלמה בהצלחה!\n📁 {out_path}\n📦 גודל: {file_size_mb:.1f} MB",
            "ffmpeg_cmd": ff_cmd,
            "output": out_path,
            "file_size_mb": f"{file_size_mb:.1f}"
        }
    else:
        # Either FFmpeg missing or disabled or failed - create .cmd for manual run
        bat_path = os.path.join(out_dir, "run_ffmpeg.cmd")
        try:
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(ff_cmd + "\n")
        except Exception:
            pass
        status = "ok" if (not run_now or not has_ffmpeg) else "error"
        emoji = "✅" if status == "ok" else "❌"
        
        if not has_ffmpeg:
            details = f"⚠️ FFmpeg לא זמין במערכת\n📄 נוצר קובץ CMD: {bat_path}"
        elif not run_now:
            details = f"📄 נוצר קובץ CMD להרצה ידנית: {bat_path}"
        else:
            details = f"❌ FFmpeg נכשל עם קוד {retcode}\n📄 ניתן להריץ ידנית: {bat_path}"
            
        return {
            "url": page_url,
            "title": title_http or "(ללא כותרת)",
            "status": status,
            "emoji": emoji,
            "details": details,
            "ffmpeg_cmd": ff_cmd,
            "output": out_path
        }

# ---------------- Routes ----------------
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/run-batch", methods=["POST"])
def run_batch():
    """SSE endpoint - runs downloads in parallel with per-item progress (yt-dlp)."""
    from flask import Response, stream_with_context
    import json

    data = request.get_json(force=True)
    out_dir = data.get("out_dir") or "."
    urls = [u.strip() for u in (data.get("urls") or []) if u.strip()]
    run_now = data.get("run_now", True)  # Default to True - always download!
    concurrency = int(data.get("concurrency") or 2)

    if not urls:
        return jsonify({"ok": False, "error": "לא סופקו קישורים."}), 400

    def generate():
        q = queue.Queue()

        def make_progress_cb(idx):
            def _cb(msg: dict):
                q.put({"index": idx, "type": "progress", **msg})
            return _cb

        def worker(idx, url):
            # title via HTTP for fast UI fill
            title = extract_title_via_http(url) or "(ללא כותרת)"
            q.put({"index": idx, "type": "title", "title": title, "url": url})
            
            # Try yt-dlp first (fast, generic)
            try:
                print(f"[Worker {idx}] מנסה yt-dlp עבור: {url}")
                res = download_with_ytdlp(url, out_dir=out_dir, progress_cb=make_progress_cb(idx))
                q.put({"index": idx, "type": "result", "emoji": "✅", "status": "ok", **res, "url": url})
                print(f"[Worker {idx}] ✅ הצלחה עם yt-dlp")
            except Exception as yt_error:
                print(f"[Worker {idx}] ❌ yt-dlp נכשל: {yt_error}")
                print(f"[Worker {idx}] 🔄 מנסה Selenium כ-fallback...")
                q.put({"index": idx, "type": "progress", "status": "fallback", "percent": 0, "filename": "מנסה Selenium..."})
                
                # Fallback to Selenium (slower but works for Kaltura/13tv)
                try:
                    def selenium_progress(msg):
                        q.put({"index": idx, "type": "progress", "status": "selenium", "percent": None, "filename": msg})
                    
                    res = process_single_url(url, out_dir=out_dir, run_now=run_now, progress_callback=selenium_progress)
                    if res.get("status") == "ok":
                        q.put({"index": idx, "type": "result", "emoji": "✅", **res, "url": url})
                        print(f"[Worker {idx}] ✅ הצלחה עם Selenium")
                    else:
                        q.put({"index": idx, "type": "result", "emoji": "❌", **res, "url": url})
                        print(f"[Worker {idx}] ❌ Selenium נכשל")
                except Exception as selenium_error:
                    print(f"[Worker {idx}] ❌ גם Selenium נכשל: {selenium_error}")
                    error_msg = f"yt-dlp: {str(yt_error)}\n\nSelenium fallback: {str(selenium_error)}"
                    q.put({"index": idx, "type": "result", "emoji": "❌", "status": "error", "details": error_msg, "url": url, "title": title})

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            futures = []
            for idx, u in enumerate(urls):
                futures.append(ex.submit(worker, idx, u))

            # While any worker is alive, drain queue and yield SSE
            alive = True
            while alive:
                try:
                    item = q.get(timeout=0.2)
                    yield f"data: {json.dumps(item)}\n\n"
                except Exception:
                    pass
                # Check if all futures done
                alive = any(not f.done() for f in futures)

        # flush remaining
        while not q.empty():
            item = q.get()
            yield f"data: {json.dumps(item)}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

if __name__ == "__main__":
    # Disable debug mode to prevent socket issues with multiple Selenium instances
    app.run(debug=False, threaded=True, host='127.0.0.1', port=5000)
