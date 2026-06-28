"""
nse_client.py — the ONLY file that knows HOW to talk to nseindia.com.

NSE has no free official API and actively blocks scrapers with Akamai. Two walls
we learned to get past (see backend/spikes/ for the recon that proved this):

  1. TLS fingerprint: plain `requests` is 403'd at the homepage because Akamai
     detects it isn't a real browser by its TLS handshake. We use `curl_cffi`
     with impersonate="chrome", which mimics Chrome's exact TLS fingerprint.
  2. Unvalidated cookies: even with the right TLS, the API endpoints reject you
     until you've "warmed" the session — visit the homepage (to get the Akamai
     cookies _abck/bm_sz/ak_bmsc), then visit the actual page an API is called
     from, and often RETRY once or twice before the _abck cookie validates.

Everything NSE-specific lives here. nse_provider.py asks this client for JSON or
bytes and never sees a cookie, a User-Agent, or a retry loop. If NSE changes its
defences, THIS file changes — the provider and the rest of the app do not.
"""

import logging
import time

from curl_cffi import requests as creq

log = logging.getLogger(__name__)

HOME = "https://www.nseindia.com"

# Pages to visit during warm-up so the API calls look like they come from a real
# browsing session. Order matters: homepage first (sets the Akamai cookies).
_WARM_PAGES = (
    HOME,
    "https://www.nseindia.com/market-data/live-equity-market",
    "https://www.nseindia.com/option-chain",
)


class NSEError(RuntimeError):
    """Raised when NSE can't be reached or keeps refusing after retries.
    Callers should catch this and fall back, never crash."""


class NSEClient:
    def __init__(self, impersonate: str = "chrome", warm_ttl_s: int = 600):
        self._impersonate = impersonate
        self._warm_ttl_s = warm_ttl_s   # re-warm the session at most this often
        self._session: creq.Session | None = None
        self._warmed_at: float = 0.0

    # -- session lifecycle -------------------------------------------------

    def _ensure_session(self) -> creq.Session:
        """Return a warmed session, creating/re-warming if stale or missing."""
        fresh = self._session is None or (time.time() - self._warmed_at) > self._warm_ttl_s
        if fresh:
            self._warm()
        return self._session  # type: ignore[return-value]

    def _warm(self) -> None:
        log.info("NSE: warming a new browser-impersonated session")
        s = creq.Session(impersonate=self._impersonate)
        try:
            for page in _WARM_PAGES:
                s.get(page, timeout=15)
                time.sleep(0.5)
        except Exception as e:  # noqa: BLE001 - any network error = not warmed
            raise NSEError(f"warm-up failed: {type(e).__name__}: {e}") from e
        self._session = s
        self._warmed_at = time.time()

    def _reset(self) -> None:
        self._session = None  # force a fresh warm on next call

    # -- request helpers ---------------------------------------------------

    def get_json(self, url: str, referer: str = HOME + "/", tries: int = 4) -> dict | list:
        """GET a JSON endpoint, retrying through the Akamai cookie validation.
        On a 401/403 mid-stream we re-warm the session once and keep trying."""
        last = ""
        for i in range(1, tries + 1):
            s = self._ensure_session()
            try:
                r = s.get(url, headers={"Referer": referer}, timeout=20)
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                self._reset()
                time.sleep(1.0)
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as e:  # noqa: BLE001
                    raise NSEError(f"bad JSON from {url}: {e}") from e
            last = f"HTTP {r.status_code}"
            # 401/403 => cookie likely went stale; rebuild the session.
            if r.status_code in (401, 403):
                self._reset()
            time.sleep(1.2)
        raise NSEError(f"GET {url} failed after {tries} tries (last: {last})")

    def get_bytes(self, url: str, referer: str = HOME + "/", tries: int = 3) -> bytes:
        """GET a binary file (e.g. a bhavcopy zip) with the same retry logic."""
        last = ""
        for i in range(1, tries + 1):
            s = self._ensure_session()
            try:
                r = s.get(url, headers={"Referer": referer}, timeout=30)
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                self._reset()
                time.sleep(1.0)
                continue
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code in (401, 403):
                self._reset()
            time.sleep(1.0)
        raise NSEError(f"GET {url} failed after {tries} tries (last: {last})")
