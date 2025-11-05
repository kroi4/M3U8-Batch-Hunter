"""
M3U8 Capture using Selenium 4 + CDP

Alternative m3u8 capture using Selenium 4's CDP (Chrome DevTools Protocol) support.
Lighter than selenium-wire (no MITM proxy), heavier than Playwright.

Security features:
- Direct CDP access (no proxy)
- Headless mode support
- Resource usage controls

Performance features:
- CDP Network events listening
- Browser instance reuse
- Efficient log parsing

Author: AI Assistant
License: MIT
"""

import time
import json
import logging
from typing import List, Optional, Dict, Any
from contextlib import contextmanager

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.common.exceptions import WebDriverException
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

from m3u8_sniffer_utils import (
    M3U8Info,
    is_m3u8_url,
    is_hls_content_type,
    analyze_m3u8_url,
    filter_and_prioritize_m3u8s,
)

# Configure logging
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

DEFAULT_CHROME_ARGS = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-sync',
    '--metrics-recording-only',
    '--disable-default-apps',
    '--mute-audio',
    '--autoplay-policy=no-user-gesture-required',
    '--disable-notifications',
]

# ============================================================================
# Browser Manager
# ============================================================================

class SeleniumCDPBrowserManager:
    """
    Manages a reusable Selenium WebDriver with CDP enabled.
    """
    
    def __init__(self, headless: bool = True, chrome_args: Optional[List[str]] = None):
        """
        Initialize browser manager.
        
        Args:
            headless: Run in headless mode
            chrome_args: Custom Chrome arguments
        """
        if not SELENIUM_AVAILABLE:
            raise ImportError(
                "Selenium is not installed. "
                "Install with: pip install selenium webdriver-manager"
            )
        
        self.headless = headless
        self.chrome_args = chrome_args or DEFAULT_CHROME_ARGS
        self.driver = None
        
        logger.info("SeleniumCDPBrowserManager initialized")
    
    def start(self):
        """Start the Chrome WebDriver with CDP enabled."""
        if self.driver is not None:
            logger.warning("Driver already started")
            return
        
        logger.info("Starting Selenium Chrome WebDriver with CDP...")
        
        options = ChromeOptions()
        
        if self.headless:
            options.add_argument('--headless=new')
        
        for arg in self.chrome_args:
            options.add_argument(arg)
        
        # Enable performance logging for CDP Network events
        options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
        
        try:
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
            
            # Enable CDP Network domain
            self.driver.execute_cdp_cmd('Network.enable', {})
            
            logger.info("✅ Selenium Chrome WebDriver started successfully")
        except Exception as e:
            logger.error(f"❌ Failed to start WebDriver: {e}")
            raise
    
    def stop(self):
        """Stop the WebDriver."""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception as e:
                logger.warning(f"Error closing WebDriver: {e}")
            finally:
                self.driver = None
    
    def get_driver(self):
        """Get the WebDriver instance, starting if necessary."""
        if self.driver is None:
            self.start()
        return self.driver
    
    def __enter__(self):
        """Support context manager protocol."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Support context manager protocol."""
        self.stop()


# ============================================================================
# M3U8 Capture via CDP
# ============================================================================

def capture_m3u8_via_selenium_cdp(
    url: str,
    driver_manager: Optional[SeleniumCDPBrowserManager] = None,
    timeout: int = 20,
    wait_after_load: float = 3.0,
    click_play_button: bool = True
) -> List[M3U8Info]:
    """
    Capture m3u8 URLs using Selenium + CDP.
    
    Listens to Network.responseReceived events via Chrome DevTools Protocol
    to detect m3u8 URLs.
    
    Args:
        url: Page URL to capture from
        driver_manager: Optional reusable driver manager
        timeout: Navigation timeout in seconds
        wait_after_load: Additional wait time after page load
        click_play_button: Whether to attempt clicking play buttons
        
    Returns:
        List of M3U8Info objects
        
    Raises:
        ImportError: If Selenium is not installed
        Exception: On navigation or capture errors
    """
    if not SELENIUM_AVAILABLE:
        raise ImportError(
            "Selenium is not installed. "
            "Install with: pip install selenium webdriver-manager"
        )
    
    logger.info(f"🎬 Starting m3u8 capture (Selenium+CDP) for: {url}")
    
    found_m3u8s: List[M3U8Info] = []
    master_found_time = None
    
    # Use provided driver manager or create temporary one
    if driver_manager:
        own_manager = False
        manager = driver_manager
    else:
        own_manager = True
        manager = SeleniumCDPBrowserManager()
        manager.start()
    
    try:
        driver = manager.get_driver()
        driver.set_page_load_timeout(timeout)
        
        # Navigate to page
        logger.info(f"📄 Navigating to: {url}")
        try:
            driver.get(url)
            logger.info("✅ Page loaded")
        except Exception as e:
            logger.warning(f"⚠️ Page load timeout/error: {e}")
            # Continue anyway
        
        # Try to click play buttons
        if click_play_button:
            try:
                _click_play_buttons_selenium(driver)
            except Exception as e:
                logger.debug(f"Error clicking play buttons: {e}")
        
        # Poll performance logs for Network events
        logger.info(f"⏳ Polling network events for {wait_after_load}s...")
        start_time = time.time()
        check_interval = 0.3
        
        while time.time() - start_time < wait_after_load:
            # Process performance logs
            try:
                logs = driver.get_log('performance')
                
                for entry in logs:
                    try:
                        log_message = json.loads(entry['message'])['message']
                        method = log_message.get('method', '')
                        
                        # We're interested in Network.responseReceived events
                        if method == 'Network.responseReceived':
                            params = log_message.get('params', {})
                            response = params.get('response', {})
                            
                            response_url = response.get('url', '')
                            status = response.get('status', 0)
                            headers = response.get('headers', {})
                            
                            # Only process successful responses
                            if status < 200 or status >= 400:
                                continue
                            
                            # Check if this is m3u8
                            content_type = headers.get('content-type', '').lower()
                            content_type = content_type or headers.get('Content-Type', '').lower()
                            
                            is_m3u8_by_url = is_m3u8_url(response_url)
                            is_m3u8_by_content = is_hls_content_type(content_type)
                            
                            if is_m3u8_by_url or is_m3u8_by_content:
                                # Get request info if available
                                request_id = params.get('requestId')
                                frame_id = params.get('frameId')
                                
                                # Analyze and store
                                m3u8_info = analyze_m3u8_url(
                                    url=response_url,
                                    timestamp=time.time(),
                                    response_headers=headers,
                                    status_code=status,
                                    initiator=None  # Could be extracted from Network.requestWillBeSent
                                )
                                
                                # Avoid duplicates
                                if not any(m.url == m3u8_info.url for m in found_m3u8s):
                                    found_m3u8s.append(m3u8_info)
                                    
                                    detection_method = "URL" if is_m3u8_by_url else "Content-Type"
                                    logger.info(
                                        f"✅ Detected m3u8 via {detection_method}: {response_url[:80]}... "
                                        f"(master={m3u8_info.is_master})"
                                    )
                                    
                                    # Early exit if master found
                                    if m3u8_info.is_master and master_found_time is None:
                                        master_found_time = time.time()
                                        logger.info("🎯 Master manifest detected! Will exit early.")
                    
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        logger.debug(f"Error parsing log entry: {e}")
                        continue
            
            except Exception as e:
                logger.debug(f"Error getting performance logs: {e}")
            
            # Early exit if master found
            if master_found_time and (time.time() - master_found_time > 1.0):
                logger.info("🚀 Master manifest found, exiting early!")
                break
            
            time.sleep(check_interval)
        
        logger.info(f"✅ Capture complete. Found {len(found_m3u8s)} m3u8 URLs")
    
    finally:
        # Clean up if we created our own manager
        if own_manager:
            manager.stop()
    
    return found_m3u8s


def _click_play_buttons_selenium(driver):
    """
    Attempt to click play buttons using Selenium.
    
    Args:
        driver: Selenium WebDriver
    """
    from selenium.webdriver.common.by import By
    
    logger.debug("🎬 Attempting to click play buttons...")
    
    # Common play button selectors
    play_button_selectors = [
        (By.CSS_SELECTOR, 'button.playkit-pre-playback-play-button'),
        (By.CSS_SELECTOR, 'button.vjs-big-play-button'),
        (By.CSS_SELECTOR, 'button[title="Play"]'),
        (By.CSS_SELECTOR, 'button[aria-label*="Play"]'),
        (By.CSS_SELECTOR, 'button[aria-label*="נגן"]'),
        (By.CSS_SELECTOR, '.play-button'),
        (By.XPATH, '//button[contains(@class, "play")]'),
    ]
    
    for by, selector in play_button_selectors:
        try:
            elements = driver.find_elements(by, selector)
            for element in elements:
                try:
                    if element.is_displayed():
                        element.click()
                        logger.debug(f"✅ Clicked play button: {selector}")
                        time.sleep(0.5)
                        return  # Exit after first successful click
                except Exception:
                    continue
        except Exception:
            continue
    
    # Try JavaScript video.play()
    try:
        driver.execute_script("""
            (function() {
                var videos = document.getElementsByTagName('video');
                for (var i = 0; i < videos.length; i++) {
                    try {
                        videos[i].muted = true;
                        videos[i].play();
                    } catch(e) {}
                }
            })();
        """)
        logger.debug("📹 Attempted to start video elements via JS")
    except Exception:
        pass


# ============================================================================
# High-level API
# ============================================================================

def capture_m3u8_from_urls_selenium(
    urls: List[str],
    timeout: int = 20,
    wait_after_load: float = 3.0,
    reuse_driver: bool = True
) -> Dict[str, List[M3U8Info]]:
    """
    Capture m3u8 URLs from multiple pages using Selenium+CDP.
    
    Args:
        urls: List of page URLs to process
        timeout: Navigation timeout per URL
        wait_after_load: Wait time after page load
        reuse_driver: Whether to reuse driver instance
        
    Returns:
        Dictionary mapping URL -> List[M3U8Info]
    """
    results = {}
    
    if reuse_driver:
        logger.info(f"🚀 Processing {len(urls)} URLs with driver reuse")
        with SeleniumCDPBrowserManager() as manager:
            for url in urls:
                try:
                    m3u8s = capture_m3u8_via_selenium_cdp(
                        url,
                        driver_manager=manager,
                        timeout=timeout,
                        wait_after_load=wait_after_load
                    )
                    results[url] = m3u8s
                except Exception as e:
                    logger.error(f"❌ Error processing {url}: {e}")
                    results[url] = []
    else:
        logger.info(f"🚀 Processing {len(urls)} URLs with fresh drivers")
        for url in urls:
            try:
                m3u8s = capture_m3u8_via_selenium_cdp(
                    url,
                    driver_manager=None,
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

def capture_m3u8_via_cdp_selenium(
    url: str,
    browser_or_driver=None,
    timeout: int = 20
) -> List[Dict[str, Any]]:
    """
    Capture m3u8 URLs using CDP via Selenium 4.
    
    Convenience wrapper matching the specification.
    
    Args:
        url: Page URL to capture from
        browser_or_driver: Optional SeleniumCDPBrowserManager for reuse
        timeout: Timeout in seconds
        
    Returns:
        List of dictionaries with m3u8 metadata
    """
    m3u8_infos = capture_m3u8_via_selenium_cdp(
        url=url,
        driver_manager=browser_or_driver,
        timeout=timeout,
        wait_after_load=3.0
    )
    
    # Sort by priority
    prioritized = filter_and_prioritize_m3u8s(m3u8_infos)
    
    # Return as dictionaries
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

