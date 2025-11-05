"""
Test M3U8 Capture with Local HTTP Server

This test spins up a local HTTP server serving sample HLS manifests
and verifies that the capture functions can detect them.

Usage:
    python test_capture_local_m3u8.py

Requirements:
    - Playwright or Selenium installed
    - Port 8888 available

Author: AI Assistant
License: MIT
"""

import os
import sys
import time
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
import tempfile
import shutil
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Sample HLS manifests
MASTER_MANIFEST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"
variant_720p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
variant_1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"
variant_360p.m3u8
"""

VARIANT_720P = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:10.0,
segment0.ts
#EXTINF:10.0,
segment1.ts
#EXTINF:10.0,
segment2.ts
#EXTINF:5.0,
segment3.ts
#EXT-X-ENDLIST
"""

VARIANT_1080P = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:10.0,
segment0_hd.ts
#EXTINF:10.0,
segment1_hd.ts
#EXTINF:10.0,
segment2_hd.ts
#EXTINF:5.0,
segment3_hd.ts
#EXT-X-ENDLIST
"""

VARIANT_360P = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:10.0,
segment0_sd.ts
#EXTINF:10.0,
segment1_sd.ts
#EXTINF:10.0,
segment2_sd.ts
#EXTINF:5.0,
segment3_sd.ts
#EXT-X-ENDLIST
"""

HTML_PAGE_WITH_VIDEO = """<!DOCTYPE html>
<html>
<head>
    <title>Test HLS Video Player</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
</head>
<body>
    <h1>Test HLS Video</h1>
    <video id="video" controls width="640" height="360"></video>
    
    <script>
        var video = document.getElementById('video');
        var videoSrc = '/master.m3u8';
        
        if (Hls.isSupported()) {
            var hls = new Hls();
            hls.loadSource(videoSrc);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, function() {
                console.log('HLS manifest loaded');
            });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = videoSrc;
        }
    </script>
    
    <p>Master manifest: <a href="/master.m3u8">master.m3u8</a></p>
    <p>Variant 1080p: <a href="/variant_1080p.m3u8">variant_1080p.m3u8</a></p>
    <p>Variant 720p: <a href="/variant_720p.m3u8">variant_720p.m3u8</a></p>
    <p>Variant 360p: <a href="/variant_360p.m3u8">variant_360p.m3u8</a></p>
</body>
</html>
"""


class M3U8Handler(SimpleHTTPRequestHandler):
    """Custom handler that serves m3u8 files with correct Content-Type."""
    
    def end_headers(self):
        """Add CORS and Content-Type headers."""
        self.send_header('Access-Control-Allow-Origin', '*')
        if self.path.endswith('.m3u8'):
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
        SimpleHTTPRequestHandler.end_headers(self)
    
    def log_message(self, format, *args):
        """Log requests."""
        logger.debug(f"[HTTP] {format % args}")


def setup_test_server(port=8888):
    """
    Set up a local HTTP server with sample HLS files.
    
    Returns:
        Tuple of (server, temp_dir, base_url)
    """
    # Create temporary directory
    temp_dir = tempfile.mkdtemp(prefix='m3u8_test_')
    logger.info(f"Created temp directory: {temp_dir}")
    
    # Write manifest files
    with open(os.path.join(temp_dir, 'master.m3u8'), 'w') as f:
        f.write(MASTER_MANIFEST)
    
    with open(os.path.join(temp_dir, 'variant_720p.m3u8'), 'w') as f:
        f.write(VARIANT_720P)
    
    with open(os.path.join(temp_dir, 'variant_1080p.m3u8'), 'w') as f:
        f.write(VARIANT_1080P)
    
    with open(os.path.join(temp_dir, 'variant_360p.m3u8'), 'w') as f:
        f.write(VARIANT_360P)
    
    with open(os.path.join(temp_dir, 'index.html'), 'w') as f:
        f.write(HTML_PAGE_WITH_VIDEO)
    
    logger.info("Created test manifest files")
    
    # Create HTTP server
    os.chdir(temp_dir)
    server = HTTPServer(('127.0.0.1', port), M3U8Handler)
    base_url = f"http://127.0.0.1:{port}"
    
    logger.info(f"✅ Test server ready at {base_url}")
    
    return server, temp_dir, base_url


def run_server(server):
    """Run server in thread."""
    logger.info("Starting HTTP server thread...")
    server.serve_forever()


def test_playwright_capture(base_url):
    """Test Playwright capture."""
    logger.info("=" * 60)
    logger.info("TEST: Playwright M3U8 Capture")
    logger.info("=" * 60)
    
    try:
        from m3u8_capture_playwright import (
            capture_m3u8_via_playwright,
            PLAYWRIGHT_AVAILABLE
        )
        
        if not PLAYWRIGHT_AVAILABLE:
            logger.warning("⚠️ Playwright not available, skipping test")
            return False
        
        test_url = f"{base_url}/index.html"
        logger.info(f"Testing URL: {test_url}")
        
        # Capture m3u8 URLs
        results = capture_m3u8_via_playwright(
            url=test_url,
            timeout=10,
            wait_after_load=2.0
        )
        
        # Verify results
        logger.info(f"Captured {len(results)} m3u8 URLs")
        
        for m3u8 in results:
            logger.info(f"  - {m3u8.url} (master={m3u8.is_master})")
        
        # Check that we found the master manifest
        master_found = any(m.is_master and 'master.m3u8' in m.url for m in results)
        
        if master_found:
            logger.info("✅ TEST PASSED: Master manifest detected!")
            return True
        else:
            logger.warning("❌ TEST FAILED: Master manifest not detected")
            return False
    
    except ImportError as e:
        logger.warning(f"⚠️ Playwright not installed: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ TEST FAILED: {e}", exc_info=True)
        return False


def test_selenium_cdp_capture(base_url):
    """Test Selenium CDP capture."""
    logger.info("=" * 60)
    logger.info("TEST: Selenium CDP M3U8 Capture")
    logger.info("=" * 60)
    
    try:
        from m3u8_capture_selenium_cdp import capture_m3u8_via_selenium_cdp
        
        test_url = f"{base_url}/index.html"
        logger.info(f"Testing URL: {test_url}")
        
        # Capture m3u8 URLs
        results = capture_m3u8_via_selenium_cdp(
            url=test_url,
            timeout=10,
            wait_after_load=2.0
        )
        
        # Verify results
        logger.info(f"Captured {len(results)} m3u8 URLs")
        
        for m3u8 in results:
            logger.info(f"  - {m3u8.url} (master={m3u8.is_master})")
        
        # Check that we found at least one m3u8
        if len(results) > 0:
            logger.info("✅ TEST PASSED: M3U8 URLs detected!")
            return True
        else:
            logger.warning("❌ TEST FAILED: No m3u8 URLs detected")
            return False
    
    except ImportError as e:
        logger.warning(f"⚠️ Selenium not installed: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ TEST FAILED: {e}", exc_info=True)
        return False


def main():
    """Main test runner."""
    logger.info("🚀 Starting M3U8 Capture Local Test Suite")
    
    # Start test server
    server, temp_dir, base_url = setup_test_server(port=8888)
    server_thread = threading.Thread(target=run_server, args=(server,), daemon=True)
    server_thread.start()
    
    # Wait for server to be ready
    time.sleep(1)
    
    try:
        # Run tests
        results = {}
        
        # Test Playwright
        results['playwright'] = test_playwright_capture(base_url)
        
        # Test Selenium CDP
        results['selenium_cdp'] = test_selenium_cdp_capture(base_url)
        
        # Summary
        logger.info("=" * 60)
        logger.info("TEST SUMMARY")
        logger.info("=" * 60)
        
        for test_name, passed in results.items():
            status = "✅ PASSED" if passed else "❌ FAILED"
            logger.info(f"{test_name}: {status}")
        
        all_passed = all(results.values())
        
        if all_passed:
            logger.info("\n🎉 All tests PASSED!")
            return 0
        else:
            logger.warning("\n⚠️ Some tests FAILED")
            return 1
    
    finally:
        # Cleanup
        logger.info("Cleaning up...")
        server.shutdown()
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"Removed temp directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to remove temp directory: {e}")


if __name__ == '__main__':
    sys.exit(main())

