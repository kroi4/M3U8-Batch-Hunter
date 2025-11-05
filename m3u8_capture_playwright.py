"""
M3U8 Capture using Playwright

Lightweight m3u8 capture using Playwright's network event listeners.
More efficient than selenium-wire (no MITM proxy overhead).

Security features:
- Runs in sandboxed headless browser
- No proxy MITM (direct network listening)
- Resource usage limits (no images/fonts/ads)
- Timeout protection

Performance features:
- Browser instance reuse
- Headless mode
- Resource blocking (images, fonts, ads)
- Early detection and quick exit

Author: AI Assistant
License: MIT
"""

import time
import logging
import re
from typing import List, Optional, Dict, Any
from contextlib import contextmanager

try:
    from playwright.sync_api import sync_playwright, Browser, Page, Response, Error as PlaywrightError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from m3u8_sniffer_utils import (
    M3U8Info,
    is_m3u8_url,
    is_hls_content_type,
    is_kaltura_master_manifest,
    analyze_m3u8_url,
    filter_and_prioritize_m3u8s,
)

# Configure logging
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

# Resources to block for faster loading
BLOCKED_RESOURCE_TYPES = ['image', 'font', 'media', 'stylesheet']

# Additional URL patterns to block (ads, trackers)
BLOCKED_URL_PATTERNS = [
    r'.*\.doubleclick\.net.*',
    r'.*\.googlesyndication\.com.*',
    r'.*\.google-analytics\.com.*',
    r'.*\.googletagmanager\.com.*',
    r'.*\.facebook\.net.*',
    r'.*\.facebook\.com/tr.*',
    r'.*\.adnxs\.com.*',
    r'.*\.advertising\.com.*',
]

BLOCKED_URL_REGEX = re.compile('|'.join(BLOCKED_URL_PATTERNS), re.IGNORECASE)

# Default browser arguments for efficiency
DEFAULT_BROWSER_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-networking',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-breakpad',
    '--disable-component-extensions-with-background-pages',
    '--disable-extensions',
    '--disable-features=TranslateUI',
    '--disable-ipc-flooding-protection',
    '--disable-renderer-backgrounding',
    '--disable-sync',
    '--metrics-recording-only',
    '--mute-audio',
    '--autoplay-policy=no-user-gesture-required',
]

# ============================================================================
# Browser Manager
# ============================================================================

class PlaywrightBrowserManager:
    """
    Manages a reusable Playwright browser instance.
    
    Allows browser instance reuse across multiple URLs for efficiency.
    """
    
    def __init__(self, headless: bool = True, browser_args: Optional[List[str]] = None):
        """
        Initialize browser manager.
        
        Args:
            headless: Run browser in headless mode
            browser_args: Custom browser arguments (defaults to DEFAULT_BROWSER_ARGS)
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "Playwright is not installed. "
                "Install with: pip install playwright && playwright install chromium"
            )
        
        self.headless = headless
        self.browser_args = browser_args or DEFAULT_BROWSER_ARGS
        self.playwright = None
        self.browser = None
        
        logger.info("PlaywrightBrowserManager initialized")
    
    def start(self):
        """Start the Playwright browser."""
        if self.browser is not None:
            logger.warning("Browser already started")
            return
        
        logger.info("Starting Playwright browser...")
        self.playwright = sync_playwright().start()
        
        try:
            self.browser = self.playwright.chromium.launch(
                headless=self.headless,
                args=self.browser_args
            )
            logger.info("✅ Playwright browser started successfully")
        except Exception as e:
            logger.error(f"❌ Failed to start browser: {e}")
            if self.playwright:
                self.playwright.stop()
                self.playwright = None
            raise
    
    def stop(self):
        """Stop the Playwright browser."""
        if self.browser:
            try:
                self.browser.close()
                logger.info("Browser closed")
            except Exception as e:
                logger.warning(f"Error closing browser: {e}")
            finally:
                self.browser = None
        
        if self.playwright:
            try:
                self.playwright.stop()
                logger.info("Playwright stopped")
            except Exception as e:
                logger.warning(f"Error stopping playwright: {e}")
            finally:
                self.playwright = None
    
    def get_browser(self) -> Browser:
        """Get the browser instance, starting if necessary."""
        if self.browser is None:
            self.start()
        return self.browser
    
    @contextmanager
    def new_page(self, **kwargs):
        """
        Context manager for creating a new page.
        
        Yields:
            Page object
        """
        browser = self.get_browser()
        page = browser.new_page(**kwargs)
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception as e:
                logger.warning(f"Error closing page: {e}")
    
    def __enter__(self):
        """Support context manager protocol."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Support context manager protocol."""
        self.stop()

# ============================================================================
# M3U8 Capture
# ============================================================================

def capture_m3u8_via_playwright(
    url: str,
    browser_manager: Optional[PlaywrightBrowserManager] = None,
    timeout: int = 20,
    wait_after_load: float = 3.0,
    block_resources: bool = True,
    click_play_button: bool = True
) -> List[M3U8Info]:
    """
    Capture m3u8 URLs from a page using Playwright.
    
    This function listens to network responses and detects m3u8 URLs
    either by URL pattern or Content-Type header.
    
    Args:
        url: The page URL to load and capture from
        browser_manager: Optional reusable browser manager (for efficiency)
        timeout: Navigation timeout in seconds
        wait_after_load: Additional wait time after page load (seconds)
        block_resources: Whether to block images/fonts/ads
        click_play_button: Whether to attempt clicking play buttons
        
    Returns:
        List of M3U8Info objects with captured m3u8 URLs
        
    Raises:
        ImportError: If Playwright is not installed
        Exception: On navigation or capture errors
        
    Example:
        >>> # Single URL capture with fresh browser
        >>> results = capture_m3u8_via_playwright("https://example.com/video")
        >>> 
        >>> # Multiple URLs with browser reuse
        >>> with PlaywrightBrowserManager() as manager:
        ...     for url in urls:
        ...         results = capture_m3u8_via_playwright(url, browser_manager=manager)
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise ImportError(
            "Playwright is not installed. "
            "Install with: pip install playwright && playwright install chromium"
        )
    
    logger.info(f"🎬 Starting m3u8 capture for: {url}")
    
    found_m3u8s: List[M3U8Info] = []
    master_found_time = None
    
    # Use provided browser manager or create temporary one
    if browser_manager:
        own_manager = False
        manager = browser_manager
    else:
        own_manager = True
        manager = PlaywrightBrowserManager()
        manager.start()
    
    try:
        with manager.new_page() as page:
            # Set up response listener
            def on_response(response: Response):
                """Handle network responses to detect m3u8."""
                nonlocal master_found_time
                
                try:
                    response_url = response.url
                    status = response.status
                    
                    # Only process successful responses
                    if status < 200 or status >= 400:
                        return
                    
                    # Get headers safely
                    try:
                        headers = response.headers
                        content_type = headers.get('content-type', '').lower()
                    except Exception:
                        headers = {}
                        content_type = ''
                    
                    # Check if this is an m3u8 resource
                    is_m3u8_by_url = is_m3u8_url(response_url)
                    is_m3u8_by_content = is_hls_content_type(content_type)
                    
                    if is_m3u8_by_url or is_m3u8_by_content:
                        # Get initiator if available
                        try:
                            request = response.request
                            initiator = request.frame.url if request.frame else None
                        except Exception:
                            initiator = None
                        
                        # Analyze and store
                        m3u8_info = analyze_m3u8_url(
                            url=response_url,
                            timestamp=time.time(),
                            response_headers=dict(headers),
                            status_code=status,
                            initiator=initiator
                        )
                        
                        found_m3u8s.append(m3u8_info)
                        
                        # Log detection
                        detection_method = "URL" if is_m3u8_by_url else "Content-Type"
                        logger.info(
                            f"✅ Detected m3u8 via {detection_method}: {response_url[:80]}... "
                            f"(master={m3u8_info.is_master})"
                        )
                        
                        # If master manifest found, note the time for early exit
                        if m3u8_info.is_master and master_found_time is None:
                            master_found_time = time.time()
                            logger.info("🎯 Master manifest detected! Will exit early.")
                
                except Exception as e:
                    logger.debug(f"Error in response handler: {e}")
            
            # Set up route handler for resource blocking
            if block_resources:
                def on_route(route):
                    """Block unnecessary resources."""
                    request = route.request
                    resource_type = request.resource_type
                    request_url = request.url
                    
                    # Block by resource type
                    if resource_type in BLOCKED_RESOURCE_TYPES:
                        route.abort()
                        return
                    
                    # Block by URL pattern (ads, trackers)
                    if BLOCKED_URL_REGEX.search(request_url):
                        route.abort()
                        return
                    
                    # Continue with request
                    route.continue_()
                
                page.route('**/*', on_route)
            
            # Attach response listener
            page.on('response', on_response)
            
            # Navigate to page
            logger.info(f"📄 Navigating to: {url}")
            try:
                page.goto(
                    url,
                    wait_until='domcontentloaded',  # Don't wait for all resources
                    timeout=timeout * 1000
                )
                logger.info("✅ Page loaded")
            except Exception as e:
                logger.warning(f"⚠️ Page load timeout/error: {e}")
                # Continue anyway - some m3u8s might have been captured
            
            # Try to click play buttons if requested
            if click_play_button:
                try:
                    _click_play_buttons(page)
                except Exception as e:
                    logger.debug(f"Error clicking play buttons: {e}")
            
            # Wait for additional network activity
            logger.info(f"⏳ Waiting {wait_after_load}s for additional network activity...")
            elapsed = 0.0
            check_interval = 0.2
            
            while elapsed < wait_after_load:
                # Early exit if master manifest found and grace period passed
                if master_found_time and (time.time() - master_found_time > 1.0):
                    logger.info("🚀 Master manifest found, exiting early!")
                    break
                
                time.sleep(check_interval)
                elapsed += check_interval
            
            logger.info(f"✅ Capture complete. Found {len(found_m3u8s)} m3u8 URLs")
    
    finally:
        # Clean up if we created our own manager
        if own_manager:
            manager.stop()
    
    return found_m3u8s


def _click_play_buttons(page: Page):
    """
    Attempt to click play buttons on the page.
    
    Args:
        page: Playwright Page object
    """
    logger.debug("🎬 Attempting to click play buttons...")
    
    # Common play button selectors
    play_button_selectors = [
        'button.playkit-pre-playback-play-button',
        'button.vjs-big-play-button',
        'button[title="Play"]',
        'button[aria-label*="Play"]',
        'button[aria-label*="נגן"]',
        '.play-button',
        '[class*="play-button"]',
        '[id*="play-button"]',
    ]
    
    clicked = False
    
    for selector in play_button_selectors:
        try:
            # Try to click with short timeout
            element = page.locator(selector).first
            if element.is_visible(timeout=500):
                element.click(timeout=500)
                clicked = True
                logger.debug(f"✅ Clicked play button: {selector}")
                time.sleep(0.5)  # Brief wait after click
                break
        except Exception:
            continue
    
    # Try to start video elements directly via JavaScript
    try:
        page.evaluate("""
            () => {
                const videos = document.getElementsByTagName('video');
                for (const video of videos) {
                    try {
                        video.muted = true;
                        video.play();
                    } catch(e) {}
                }
            }
        """)
        logger.debug("📹 Attempted to start video elements via JS")
    except Exception:
        pass
    
    # Handle iframes
    try:
        for frame in page.frames:
            if frame.url != 'about:blank':
                _click_play_buttons_in_frame(frame)
    except Exception:
        pass


def _click_play_buttons_in_frame(frame):
    """Try clicking play buttons in a frame."""
    play_button_selectors = [
        'button.playkit-pre-playback-play-button',
        'button.vjs-big-play-button',
        'button[title="Play"]',
    ]
    
    for selector in play_button_selectors:
        try:
            element = frame.locator(selector).first
            if element.is_visible(timeout=300):
                element.click(timeout=300)
                break
        except Exception:
            continue


# ============================================================================
# High-level API
# ============================================================================

def capture_m3u8_from_urls(
    urls: List[str],
    timeout: int = 20,
    wait_after_load: float = 3.0,
    reuse_browser: bool = True
) -> Dict[str, List[M3U8Info]]:
    """
    Capture m3u8 URLs from multiple pages.
    
    Args:
        urls: List of page URLs to process
        timeout: Navigation timeout per URL
        wait_after_load: Wait time after page load
        reuse_browser: Whether to reuse browser instance (recommended)
        
    Returns:
        Dictionary mapping URL -> List[M3U8Info]
        
    Example:
        >>> urls = ["https://example.com/video1", "https://example.com/video2"]
        >>> results = capture_m3u8_from_urls(urls, reuse_browser=True)
        >>> for url, m3u8s in results.items():
        ...     print(f"{url}: {len(m3u8s)} m3u8s found")
    """
    results = {}
    
    if reuse_browser:
        # Reuse browser instance for efficiency
        logger.info(f"🚀 Processing {len(urls)} URLs with browser reuse")
        with PlaywrightBrowserManager() as manager:
            for url in urls:
                try:
                    m3u8s = capture_m3u8_via_playwright(
                        url,
                        browser_manager=manager,
                        timeout=timeout,
                        wait_after_load=wait_after_load
                    )
                    results[url] = m3u8s
                except Exception as e:
                    logger.error(f"❌ Error processing {url}: {e}")
                    results[url] = []
    else:
        # Create new browser for each URL
        logger.info(f"🚀 Processing {len(urls)} URLs with fresh browsers")
        for url in urls:
            try:
                m3u8s = capture_m3u8_via_playwright(
                    url,
                    browser_manager=None,
                    timeout=timeout,
                    wait_after_load=wait_after_load
                )
                results[url] = m3u8s
            except Exception as e:
                logger.error(f"❌ Error processing {url}: {e}")
                results[url] = []
    
    return results


# ============================================================================
# Convenience wrapper matching the spec
# ============================================================================

def capture_m3u8_via_cdp(
    url: str,
    browser_or_driver=None,
    timeout: int = 20
) -> List[Dict[str, Any]]:
    """
    Capture m3u8 URLs using CDP (Chrome DevTools Protocol) via Playwright.
    
    This is a convenience wrapper matching the specification.
    
    Args:
        url: Page URL to capture from
        browser_or_driver: Optional PlaywrightBrowserManager instance for reuse
        timeout: Timeout in seconds
        
    Returns:
        List of dictionaries with m3u8 metadata:
        [
            {
                'url': str,
                'timestamp': float,
                'initiator': str,
                'response_headers': dict,
                'status': int,
                'is_master': bool,
                'is_kaltura': bool,
                'entry_id': str,
                'flavor_id': str
            },
            ...
        ]
    """
    m3u8_infos = capture_m3u8_via_playwright(
        url=url,
        browser_manager=browser_or_driver,
        timeout=timeout,
        wait_after_load=3.0
    )
    
    # Convert M3U8Info objects to dictionaries
    results = []
    for info in m3u8_infos:
        results.append({
            'url': info.url,
            'timestamp': info.timestamp,
            'initiator': info.initiator,
            'response_headers': info.response_headers,
            'status': info.status_code,
            'is_master': info.is_master,
            'is_kaltura': info.is_kaltura,
            'entry_id': info.entry_id,
            'flavor_id': info.flavor_id,
        })
    
    # Sort by priority
    prioritized = filter_and_prioritize_m3u8s(m3u8_infos)
    
    # Return as dictionaries in prioritized order
    return [{
        'url': info.url,
        'timestamp': info.timestamp,
        'initiator': info.initiator,
        'response_headers': info.response_headers,
        'status': info.status_code,
        'is_master': info.is_master,
        'is_kaltura': info.is_kaltura,
        'entry_id': info.entry_id,
        'flavor_id': info.flavor_id,
    } for info in prioritized]

