from flask import Flask, render_template, request, jsonify
import os, time, re, requests, shutil, subprocess, unicodedata, html as html_mod
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

app = Flask(__name__)

UA = "Mozilla/5.0"
DEFAULT_REFERER = None

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
                if "cdnapisec.kaltura.com" in url and "playmanifest" in url.lower():
                    master_request = req
                    
    finally:
        stop_clicking.set()
        click_thread.join(timeout=1)
    
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in driver.get_cookies()])
    
    master_manifest_url = None
    for u in found:
        if "cdnapisec.kaltura.com" in u and "playmanifest" in u.lower():
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
                    # Check if this is the master manifest
                    if "cdnapisec.kaltura.com" in url and "playmanifest" in url.lower():
                        master_request = req
                        if master_found_time is None:
                            master_found_time = time.time()
                            print(f"✅ נתפס Master Manifest! ממתין {grace_after_first} שניות נוספות...")
        except Exception:
            pass

        has_master = any(("cdnapisec.kaltura.com" in u and "playmanifest" in u.lower()) for u in found)
        
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
        if "cdnapisec.kaltura.com" in u and "playmanifest" in u.lower():
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

    # Create fresh Chrome options and driver for each URL
    log_progress("🌐 יוצר דפדפן...")
    options = webdriver.ChromeOptions()
    options.add_argument("--mute-audio")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=1366,900")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # Note: NOT using headless mode as it may interfere with video player detection
    
    # Add logging to help debug
    options.add_experimental_option('excludeSwitches', ['enable-logging'])

    driver = None
    found_m3u8 = set()
    master_manifest_url = None
    referer = page_url

    try:
        # Create driver with fresh service each time
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        
        log_progress("📄 טוען דף...")
        driver.get(page_url)
        WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(1.5)
        
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
    master = None
    for u in found_m3u8:
        if "cdnapisec.kaltura.com" in u and "playmanifest" in u.lower():
            master = u
            break

    variants = list_variants_from_master(master, referer=referer) if master else []
    if not variants:
        for u in sorted(found_m3u8):
            if "serveflavor" in u.lower() and u.lower().endswith(".m3u8"):
                variants.append({"playlist": u, "bandwidth": 0, "resolution": None, "codecs": None})

    log_progress(f"📊 מנתח {len(variants)} וריאנטי איכות...")
    rows = []
    dur_by_entry = {}
    for v in variants:
        try:
            a = analyze_variant(v["playlist"], referer=referer)
        except Exception as e:
            log_progress(f"❌ שגיאה בניתוח וריאנט: {e}")
            return {
                "url": page_url,
                "title": title_http or "(ללא כותרת)",
                "status": "error",
                "emoji": "❌",
                "details": f"Failed analyzing variant: {e}",
            }
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
        log_progress("❌ לא נמצאו וריאנטים")
        return {"url": page_url, "title": title_http or "(ללא כותרת)", "status": "error", "emoji": "❌", "details": "לא נמצאו וריאנטים להמרה."}

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
        try:
            retcode = subprocess.call(ff_cmd, shell=True)
            ran = True
            log_progress(f"✅ FFmpeg הסתיים עם קוד {retcode}")
        except Exception as e:
            log_progress(f"❌ FFmpeg נכשל: {e}")
            return {"url": page_url, "title": title_http or "(ללא כותרת)", "status": "error", "emoji": "❌", "details": f"FFmpeg error: {e}", "ffmpeg": ff_cmd}

    if run_now and has_ffmpeg and ran and retcode == 0 and os.path.exists(out_path):
        return {
            "url": page_url,
            "title": title_http or "(ללא כותרת)",
            "status": "ok",
            "emoji": "✅",
            "details": f"נקלט ונשמר: {out_path}",
            "ffmpeg": ff_cmd,
            "output": out_path
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
        details = "נוצר קובץ CMD להרצה ידנית: " + bat_path if status == "ok" else f"FFmpeg החזיר קוד {retcode}"
        return {
            "url": page_url,
            "title": title_http or "(ללא כותרת)",
            "status": status,
            "emoji": emoji,
            "details": details,
            "ffmpeg": ff_cmd,
            "output": out_path
        }

# ---------------- Routes ----------------
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/run-batch", methods=["POST"])
def run_batch():
    """SSE endpoint - streams results as they complete"""
    from flask import Response, stream_with_context
    import json
    
    data = request.get_json(force=True)
    out_dir = data.get("out_dir") or "."
    urls = [u.strip() for u in (data.get("urls") or []) if u.strip()]
    run_now = bool(data.get("run_now"))

    if not urls:
        return jsonify({"ok": False, "error": "לא סופקו קישורים."}), 400

    def generate():
        """Generator that yields SSE formatted results"""
        import traceback
        
        for idx, u in enumerate(urls):
            # Small delay between URLs to allow cleanup
            if idx > 0:
                time.sleep(2)
            
            # Send title first (from HTTP request)
            try:
                title = extract_title_via_http(u)
                yield f"data: {json.dumps({'index': idx, 'type': 'title', 'title': title or '(ללא כותרת)', 'url': u})}\n\n"
            except Exception as e:
                title = "(ללא כותרת)"
                yield f"data: {json.dumps({'index': idx, 'type': 'title', 'title': title, 'url': u})}\n\n"
            
            # Send progress update - starting
            yield f"data: {json.dumps({'index': idx, 'type': 'progress', 'message': '🔍 מתחיל עיבוד...'})}\n\n"
            
            # Process the URL with progress callback
            # We'll use a shared list to collect progress messages
            progress_messages = []
            
            class ProgressYielder:
                """Helper class to yield progress updates in real-time"""
                def __init__(self, idx):
                    self.idx = idx
                
                def send(self, message):
                    """Send a progress message"""
                    progress_messages.append(f"data: {json.dumps({'index': self.idx, 'type': 'progress', 'message': message})}\n\n")
            
            try:
                progress_yielder = ProgressYielder(idx)
                
                # Start a separate function to yield progress messages
                import sys
                sys.stdout.flush()  # Ensure output is flushed
                
                res = process_single_url(u, out_dir=out_dir, run_now=run_now, progress_callback=progress_yielder.send)
                
                # Yield any accumulated progress messages
                for msg in progress_messages:
                    yield msg
                
                res['index'] = idx
                res['type'] = 'result'
                yield f"data: {json.dumps(res)}\n\n"
            except Exception as e:
                # Detailed error logging
                error_details = f"Exception: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
                res = {
                    "index": idx,
                    "type": "result",
                    "url": u,
                    "title": title,
                    "status": "error",
                    "emoji": "❌",
                    "details": error_details
                }
                yield f"data: {json.dumps(res)}\n\n"
        
        # Send completion signal
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

if __name__ == "__main__":
    app.run(debug=True, threaded=True)
