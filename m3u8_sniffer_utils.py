"""
m3u8 Sniffer Utilities Module

Provides helper functions for detecting, cleaning, and analyzing m3u8 URLs.
Inspired by SnifferTV Chrome extension logic.

Security considerations:
- Input validation and sanitization for URLs
- Safe regex patterns without catastrophic backtracking
- URL decoding with error handling
- Protection against malicious query parameters

Author: AI Assistant
License: MIT
"""

import re
import base64
import urllib.parse
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

# Configure logging
logger = logging.getLogger(__name__)

# ============================================================================
# Regex Patterns for m3u8 Detection
# ============================================================================

# Primary m3u8 detection regex (URL-based)
# Matches URLs ending in .m3u8 with optional query parameters
M3U8_URL_REGEX = re.compile(
    r'.*\.m3u8($|\?)',
    re.IGNORECASE
)

# Comprehensive m3u8 pattern for various edge cases
M3U8_COMPREHENSIVE_REGEX = re.compile(
    r'(?:\.m3u8|/manifest\.m3u8|playlist\.m3u8|master\.m3u8|index\.m3u8)',
    re.IGNORECASE
)

# Kaltura-specific patterns
KALTURA_PLAYMANIFEST_REGEX = re.compile(
    r'kaltura\.com.*playmanifest',
    re.IGNORECASE
)

KALTURA_ENTRYID_REGEX = re.compile(
    r'/entryId/([^/]+)',
    re.IGNORECASE
)

KALTURA_FLAVORID_REGEX = re.compile(
    r'/flavorId/([^/]+)',
    re.IGNORECASE
)

# Known CDN patterns for Kaltura
KALTURA_CDN_PATTERNS = [
    r'cdnapisec\.kaltura\.com',
    r'cfvod\.kaltura\.com',
    r'.*\.kaltura\.com',
]

KALTURA_CDN_REGEX = re.compile(
    '|'.join(f'({p})' for p in KALTURA_CDN_PATTERNS),
    re.IGNORECASE
)

# Content-Type patterns for HLS streams
HLS_CONTENT_TYPES = (
    'application/vnd.apple.mpegurl',
    'application/x-mpegurl',
    'audio/x-mpegurl',
    'audio/mpegurl',
    'application/mpegurl',
)

# ============================================================================
# URL Cleanup and Decoding
# ============================================================================

# Tracking/spam query parameters to remove
SPAM_QUERY_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'msclkid', '_ga', '_gid', 'mc_cid', 'mc_eid',
    'ref', 'source', 'campaign', 'tracker', 'tracking',
}


def is_m3u8_url(url: str) -> bool:
    """
    Check if a URL points to an m3u8 file.
    
    Args:
        url: The URL to check
        
    Returns:
        True if URL matches m3u8 patterns
        
    Example:
        >>> is_m3u8_url("https://example.com/video.m3u8")
        True
        >>> is_m3u8_url("https://example.com/video.mp4")
        False
    """
    if not url:
        return False
    return bool(M3U8_URL_REGEX.search(url)) or bool(M3U8_COMPREHENSIVE_REGEX.search(url))


def is_hls_content_type(content_type: str) -> bool:
    """
    Check if content-type indicates HLS stream.
    
    Args:
        content_type: HTTP Content-Type header value
        
    Returns:
        True if content-type matches HLS patterns
    """
    if not content_type:
        return False
    content_type_lower = content_type.lower()
    return any(ct in content_type_lower for ct in HLS_CONTENT_TYPES)


def is_kaltura_url(url: str) -> bool:
    """
    Check if URL is from Kaltura CDN.
    
    Args:
        url: The URL to check
        
    Returns:
        True if URL is from Kaltura
    """
    if not url:
        return False
    return bool(KALTURA_CDN_REGEX.search(url))


def is_kaltura_master_manifest(url: str) -> bool:
    """
    Check if URL is a Kaltura master manifest (playmanifest).
    
    Args:
        url: The URL to check
        
    Returns:
        True if URL is Kaltura master manifest
    """
    if not url:
        return False
    return bool(KALTURA_PLAYMANIFEST_REGEX.search(url))


def extract_kaltura_ids(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract entryId and flavorId from Kaltura URL.
    
    Args:
        url: Kaltura URL
        
    Returns:
        Tuple of (entryId, flavorId) or (None, None) if not found
    """
    entry_id = None
    flavor_id = None
    
    entry_match = KALTURA_ENTRYID_REGEX.search(url)
    if entry_match:
        entry_id = entry_match.group(1)
    
    flavor_match = KALTURA_FLAVORID_REGEX.search(url)
    if flavor_match:
        flavor_id = flavor_match.group(1)
    
    return entry_id, flavor_id


def decode_url_fragments(url: str) -> str:
    """
    Decode URL-encoded or base64-encoded fragments in URL.
    
    Handles common encoding schemes used to obfuscate m3u8 URLs:
    - URL encoding (%20, %2F, etc.)
    - Base64 encoding in query parameters
    
    Args:
        url: The URL to decode
        
    Returns:
        Decoded URL string
    """
    if not url:
        return url
    
    try:
        # First pass: URL decode
        decoded = urllib.parse.unquote(url)
        
        # Check for base64-encoded parameters
        # Common patterns: ?url=base64data, ?stream=base64data, ?m3u8=base64data
        parsed = urllib.parse.urlparse(decoded)
        query_params = urllib.parse.parse_qs(parsed.query)
        
        for key in ['url', 'stream', 'm3u8', 'manifest', 'playlist']:
            if key in query_params:
                value = query_params[key][0]
                # Try to decode as base64
                try:
                    # Add padding if missing
                    missing_padding = len(value) % 4
                    if missing_padding:
                        value += '=' * (4 - missing_padding)
                    
                    decoded_bytes = base64.b64decode(value, validate=True)
                    decoded_value = decoded_bytes.decode('utf-8', errors='ignore')
                    
                    # If decoded value looks like a URL, use it
                    if decoded_value.startswith(('http://', 'https://')):
                        return decoded_value
                except Exception:
                    # Not base64 or invalid, continue
                    pass
        
        return decoded
    except Exception as e:
        logger.debug(f"Failed to decode URL fragments: {e}")
        return url


def strip_tracking_params(url: str) -> str:
    """
    Remove tracking and spam query parameters from URL.
    
    Args:
        url: URL to clean
        
    Returns:
        Cleaned URL without tracking parameters
    """
    if not url:
        return url
    
    try:
        parsed = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        
        # Filter out spam parameters
        cleaned_params = {
            k: v for k, v in query_params.items() 
            if k.lower() not in SPAM_QUERY_PARAMS
        }
        
        # Rebuild query string
        new_query = urllib.parse.urlencode(cleaned_params, doseq=True)
        
        # Reconstruct URL
        cleaned_url = urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))
        
        return cleaned_url
    except Exception as e:
        logger.debug(f"Failed to strip tracking params: {e}")
        return url


def clean_m3u8_url(url: str) -> str:
    """
    Clean and normalize m3u8 URL.
    
    Performs:
    1. URL fragment decoding
    2. Tracking parameter removal
    3. Normalization
    
    Args:
        url: Raw m3u8 URL
        
    Returns:
        Cleaned URL
    """
    if not url:
        return url
    
    # Decode any encoded fragments
    decoded = decode_url_fragments(url)
    
    # Strip tracking parameters
    cleaned = strip_tracking_params(decoded)
    
    return cleaned


# ============================================================================
# M3U8 Metadata Detection
# ============================================================================

@dataclass
class M3U8Info:
    """Container for m3u8 URL metadata."""
    url: str
    timestamp: float
    initiator: Optional[str] = None
    response_headers: Optional[Dict[str, str]] = None
    status_code: Optional[int] = None
    is_master: bool = False
    is_kaltura: bool = False
    entry_id: Optional[str] = None
    flavor_id: Optional[str] = None
    is_encrypted: Optional[bool] = None
    resolution: Optional[str] = None
    bandwidth: Optional[int] = None


def detect_master_vs_variant(url: str) -> bool:
    """
    Heuristic to detect if URL is a master playlist vs variant.
    
    Master playlists typically contain:
    - "playmanifest" in Kaltura URLs
    - "master" in filename
    - "manifest" in filename
    
    Args:
        url: m3u8 URL to analyze
        
    Returns:
        True if likely a master playlist
    """
    url_lower = url.lower()
    
    # Kaltura playmanifest is always master
    if 'playmanifest' in url_lower and 'kaltura' in url_lower:
        return True
    
    # Common master playlist names
    if any(name in url_lower for name in ['master.m3u8', 'manifest.m3u8', 'playlist.m3u8']):
        return True
    
    # If URL has both entryId and no flavorId, likely master
    if 'entryid' in url_lower and 'flavorid' not in url_lower:
        return True
    
    return False


def analyze_m3u8_url(
    url: str,
    timestamp: float,
    response_headers: Optional[Dict[str, str]] = None,
    status_code: Optional[int] = None,
    initiator: Optional[str] = None
) -> M3U8Info:
    """
    Analyze m3u8 URL and extract metadata.
    
    Args:
        url: The m3u8 URL
        timestamp: Timestamp when URL was captured
        response_headers: HTTP response headers
        status_code: HTTP status code
        initiator: Request initiator URL
        
    Returns:
        M3U8Info object with extracted metadata
    """
    cleaned_url = clean_m3u8_url(url)
    is_master = detect_master_vs_variant(cleaned_url)
    is_kaltura_cdn = is_kaltura_url(cleaned_url)
    entry_id, flavor_id = extract_kaltura_ids(cleaned_url)
    
    return M3U8Info(
        url=cleaned_url,
        timestamp=timestamp,
        initiator=initiator,
        response_headers=response_headers,
        status_code=status_code,
        is_master=is_master,
        is_kaltura=is_kaltura_cdn,
        entry_id=entry_id,
        flavor_id=flavor_id
    )


# ============================================================================
# URL Filtering and Prioritization
# ============================================================================

def filter_and_prioritize_m3u8s(m3u8_list: List[M3U8Info]) -> List[M3U8Info]:
    """
    Filter and prioritize captured m3u8 URLs.
    
    Priority order:
    1. Kaltura master manifests (playmanifest)
    2. Other master playlists
    3. Highest bandwidth variants
    4. Unencrypted variants
    
    Args:
        m3u8_list: List of M3U8Info objects
        
    Returns:
        Sorted and filtered list of M3U8Info objects
    """
    if not m3u8_list:
        return []
    
    # Remove duplicates (same URL)
    seen_urls = set()
    unique_m3u8s = []
    for m3u8 in m3u8_list:
        if m3u8.url not in seen_urls:
            seen_urls.add(m3u8.url)
            unique_m3u8s.append(m3u8)
    
    # Sort by priority
    def priority_key(m3u8: M3U8Info) -> Tuple:
        """
        Return sort key tuple for prioritization.
        Lower values = higher priority.
        """
        # Priority 1: Kaltura master manifest
        is_kaltura_master = m3u8.is_kaltura and m3u8.is_master
        
        # Priority 2: Any master manifest
        
        # Priority 3: Bandwidth (higher is better, so negate)
        bandwidth = -(m3u8.bandwidth or 0)
        
        # Priority 4: Not encrypted (prefer unencrypted)
        is_encrypted = m3u8.is_encrypted or False
        
        return (
            not is_kaltura_master,  # False (0) comes before True (1)
            not m3u8.is_master,
            bandwidth,
            is_encrypted
        )
    
    sorted_m3u8s = sorted(unique_m3u8s, key=priority_key)
    
    logger.info(f"Filtered and prioritized {len(sorted_m3u8s)} unique m3u8 URLs")
    return sorted_m3u8s


def select_best_variant(m3u8_list: List[M3U8Info]) -> Optional[M3U8Info]:
    """
    Select the best m3u8 variant from a list.
    
    Prefers:
    1. Master manifests (for adaptive streaming)
    2. Highest resolution/bandwidth
    3. Unencrypted streams
    
    Args:
        m3u8_list: List of M3U8Info objects
        
    Returns:
        Best M3U8Info object or None if list is empty
    """
    if not m3u8_list:
        return None
    
    prioritized = filter_and_prioritize_m3u8s(m3u8_list)
    
    # Return the first (highest priority) item
    best = prioritized[0]
    
    logger.info(
        f"Selected best variant: {best.url[:100]}... "
        f"(master={best.is_master}, kaltura={best.is_kaltura})"
    )
    
    return best

