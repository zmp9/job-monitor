"""Single HTTP chokepoint: one place for UA, timeout, and retry policy."""
import json
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 30
RETRIES = 3


def get_bytes(url: str, headers: dict | None = None) -> bytes:
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
                return f.read()
        except urllib.error.HTTPError as e:
            # 4xx other than 429 won't fix itself; stop early.
            if e.code != 429 and 400 <= e.code < 500:
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(2 ** attempt)
    raise last


def get_json(url: str, headers: dict | None = None):
    return json.loads(get_bytes(url, headers).decode("utf-8", "replace"))


def get_text(url: str, headers: dict | None = None) -> str:
    return get_bytes(url, headers).decode("utf-8", "replace")
