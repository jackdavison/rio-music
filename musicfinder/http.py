"""Shared HTTP session with sane defaults (browser UA, retries, timeouts)."""
from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
DEFAULT_TIMEOUT = 25

_session: requests.Session | None = None


class _TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().request(*args, **kwargs)


def session() -> requests.Session:
    global _session
    if _session is None:
        s = _TimeoutSession()
        s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        })
        retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "POST"))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        _session = s
    return _session


def get(url: str, **kw) -> requests.Response:
    r = session().get(url, **kw)
    r.raise_for_status()
    return r


def get_json(url: str, **kw):
    return get(url, **kw).json()


def post_json(url: str, payload=None, **kw):
    r = session().post(url, json=payload, **kw)
    r.raise_for_status()
    return r.json()
