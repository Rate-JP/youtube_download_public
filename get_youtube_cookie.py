#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from websocket import WebSocketBadStatusException, create_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(os.getenv("COOKIE_OUTPUT_BASE_DIR", "/app")).resolve()
COOKIE_TXT_RAW = os.getenv("YOUTUBE_COOKIE_FILE", str(BASE_DIR / "youtube_cookies.txt"))
COOKIE_TXT = Path(COOKIE_TXT_RAW)
if not COOKIE_TXT.is_absolute():
    COOKIE_TXT = (BASE_DIR / COOKIE_TXT).resolve()
else:
    COOKIE_TXT = COOKIE_TXT.resolve()

DEBUG_HOST = os.getenv("CHROME_REMOTE_DEBUGGING_HOST", "127.0.0.1").strip() or "127.0.0.1"
DEBUG_PORT = int(os.getenv("CHROME_REMOTE_DEBUGGING_PORT", "9222"))
WAIT_SECONDS = int(os.getenv("CHROME_DEBUG_WAIT_SECONDS", "15"))
NAVIGATION_WAIT_SECONDS = float(os.getenv("YOUTUBE_NAVIGATION_WAIT_SECONDS", "5"))
COOKIE_DOMAIN_REGEX = os.getenv("COOKIE_DOMAIN_REGEX", "").strip()
YOUTUBE_URL = os.getenv("YOUTUBE_COOKIE_REFRESH_URL", "https://www.youtube.com/").strip() or "https://www.youtube.com/"
VERSION_URL = f"http://{DEBUG_HOST}:{DEBUG_PORT}/json/version"
TARGETS_URL = f"http://{DEBUG_HOST}:{DEBUG_PORT}/json/list"


def http_get_json(url: str, timeout: int = 5) -> Any:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


class CDPClient:
    def __init__(self, websocket_url: str, timeout: int = 10) -> None:
        self.websocket_url = websocket_url
        self.timeout = timeout
        self._seq = 0
        self._ws = None

    def __enter__(self) -> "CDPClient":
        last_error: Exception | None = None
        for kwargs in (
            {"suppress_origin": True},
            {"origin": f"http://{DEBUG_HOST}:{DEBUG_PORT}"},
            {},
        ):
            try:
                self._ws = create_connection(self.websocket_url, timeout=self.timeout, **kwargs)
                return self
            except TypeError as exc:
                last_error = exc
                logger.warning("WebSocket option was not accepted: %s", kwargs)
                continue
            except WebSocketBadStatusException as exc:
                last_error = exc
                logger.warning("WebSocket handshake failed with %s: %s", kwargs, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("WebSocket connection failed with %s: %s", kwargs, exc)
                continue

        raise RuntimeError(
            "Failed to connect to Chrome DevTools WebSocket. "
            "Origin 制限または remote debugging 設定を確認してください。"
        ) from last_error

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def call(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> Any:
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")

        self._seq += 1
        message: dict[str, Any] = {
            "id": self._seq,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id

        self._ws.send(json.dumps(message))

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            raw = self._ws.recv()
            payload = json.loads(raw)
            if payload.get("id") != self._seq:
                continue
            if "error" in payload:
                raise RuntimeError(f"CDP call failed: {method}: {payload['error']}")
            return payload.get("result")

        raise TimeoutError(f"CDP call timed out: {method}")


def wait_for_debug_endpoint() -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, WAIT_SECONDS + 1):
        try:
            info = http_get_json(VERSION_URL, timeout=3)
            if isinstance(info, dict) and info.get("webSocketDebuggerUrl"):
                return info
            raise RuntimeError("webSocketDebuggerUrl was not found")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.info("Chrome remote debugging endpoint waiting... (%s/%s)", attempt, WAIT_SECONDS)
            time.sleep(1)

    raise RuntimeError(
        "Chrome remote debugging endpoint is not available. "
        "RDP で接続して Chrome が起動していることを確認してください。"
    ) from last_error


def find_existing_youtube_target() -> dict[str, Any] | None:
    try:
        targets = http_get_json(TARGETS_URL, timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enumerate Chrome targets: %s", exc)
        return None

    if not isinstance(targets, list):
        return None

    for target in targets:
        if target.get("type") != "page":
            continue
        url = str(target.get("url", ""))
        if "youtube.com" in url:
            return target

    return None


def navigate_to_youtube(browser_ws_url: str) -> None:
    logger.info("Ensuring Chrome is navigated to %s", YOUTUBE_URL)

    existing = find_existing_youtube_target()

    if existing and existing.get("webSocketDebuggerUrl"):
        target_ws = existing["webSocketDebuggerUrl"]
        logger.info("Using existing YouTube target: %s", existing.get("url", ""))
        with CDPClient(target_ws, timeout=15) as client:
            client.call("Page.enable")
            client.call("Page.bringToFront")
            client.call("Page.navigate", {"url": YOUTUBE_URL})
        time.sleep(NAVIGATION_WAIT_SECONDS)
        return

    with CDPClient(browser_ws_url, timeout=15) as client:
        created = client.call(
            "Target.createTarget",
            {"url": YOUTUBE_URL, "newWindow": False, "background": False},
        )
        target_id = created["targetId"]
        client.call("Target.activateTarget", {"targetId": target_id})
        attached = client.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = attached["sessionId"]
        client.call("Page.enable", session_id=session_id)
        client.call("Page.bringToFront", session_id=session_id)
        client.call("Page.navigate", {"url": YOUTUBE_URL}, session_id=session_id)

    time.sleep(NAVIGATION_WAIT_SECONDS)


def fetch_cookies_via_browser_ws(browser_ws_url: str) -> list[dict[str, Any]]:
    with CDPClient(browser_ws_url, timeout=15) as client:
        try:
            version = client.call("Browser.getVersion")
            logger.info("Connected to Chrome: %s", version.get("product", "unknown"))
        except Exception:
            logger.info("Connected to Chrome browser websocket")

        for method in ("Storage.getCookies", "Network.getAllCookies"):
            try:
                result = client.call(method)
                cookies = result.get("cookies", [])
                if cookies:
                    logger.info("Fetched %s cookies via %s", len(cookies), method)
                    return cookies
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s failed: %s", method, exc)

    return []


def fetch_cookies_via_page_targets() -> list[dict[str, Any]]:
    try:
        targets = http_get_json(TARGETS_URL, timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enumerate Chrome targets: %s", exc)
        return []

    if not isinstance(targets, list):
        return []

    for target in targets:
        websocket_url = target.get("webSocketDebuggerUrl")
        target_url = target.get("url", "")
        if not websocket_url:
            continue

        try:
            with CDPClient(websocket_url, timeout=10) as client:
                result = client.call("Network.getAllCookies")
                cookies = result.get("cookies", [])
                if cookies:
                    logger.info("Fetched %s cookies via page target: %s", len(cookies), target_url)
                    return cookies
        except Exception as exc:  # noqa: BLE001
            logger.warning("Target fetch failed (%s): %s", target_url, exc)
            continue

    return []


def filter_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not COOKIE_DOMAIN_REGEX:
        return cookies

    pattern = re.compile(COOKIE_DOMAIN_REGEX)
    filtered = [cookie for cookie in cookies if pattern.search(cookie.get("domain", ""))]
    logger.info("Cookie filter applied: %s -> %s (regex=%s)", len(cookies), len(filtered), COOKIE_DOMAIN_REGEX)
    return filtered


def normalize_cookie_expiry(cookie: dict[str, Any]) -> int:
    expires = cookie.get("expires", cookie.get("expiry", 0))
    if expires in (None, "", False):
        return 0
    try:
        expiry = int(float(expires))
    except Exception:  # noqa: BLE001
        return 0
    # yt-dlp 側で invalid expires at -1 を避けるため、
    # session cookie 相当の負値は 0 に正規化する
    if expiry < 0:
        return 0
    return expiry


def netscape_line(cookie: dict[str, Any]) -> str:
    domain = str(cookie.get("domain", ""))
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    path = str(cookie.get("path", "/"))
    secure = "TRUE" if cookie.get("secure", False) else "FALSE"
    expiry = normalize_cookie_expiry(cookie)
    name = str(cookie.get("name", "")).replace("\t", "%09").replace("\n", "%0A")
    value = str(cookie.get("value", "")).replace("\t", "%09").replace("\n", "%0A")
    return f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{name}\t{value}"


def save_cookies_netscape(cookies: list[dict[str, Any]]) -> None:
    COOKIE_TXT.parent.mkdir(parents=True, exist_ok=True)
    with COOKIE_TXT.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# Generated by get_youtube_cookie.py\n\n")
        for cookie in cookies:
            f.write(netscape_line(cookie))
            f.write("\n")

    try:
        COOKIE_TXT.chmod(0o600)
    except OSError:
        pass

    logger.info("Saved %s cookies to %s", len(cookies), COOKIE_TXT)


def main() -> None:
    logger.info("=" * 60)
    logger.info("Cookie export started")
    logger.info("  BASE_DIR        : %s", BASE_DIR)
    logger.info("  COOKIE_TXT      : %s", COOKIE_TXT)
    logger.info("  DEBUG_ENDPOINT  : %s:%s", DEBUG_HOST, DEBUG_PORT)
    logger.info("  REFRESH_URL     : %s", YOUTUBE_URL)
    logger.info("  NAV_WAIT_SEC    : %s", NAVIGATION_WAIT_SECONDS)
    logger.info("  COOKIE_FILTER   : %s", COOKIE_DOMAIN_REGEX if COOKIE_DOMAIN_REGEX else "(none: export all cookies)")
    logger.info("=" * 60)

    version_info = wait_for_debug_endpoint()
    browser_ws_url = version_info["webSocketDebuggerUrl"]

    navigate_to_youtube(browser_ws_url)

    cookies = fetch_cookies_via_browser_ws(browser_ws_url)
    if not cookies:
        cookies = fetch_cookies_via_page_targets()

    if not cookies:
        raise RuntimeError(
            "No cookies were fetched from Chrome. "
            "Chrome が起動中か、RDP セッションでログイン済みかを確認してください。"
        )

    cookies = filter_cookies(cookies)
    if not cookies:
        raise RuntimeError("Cookie filter resulted in zero cookies. COOKIE_DOMAIN_REGEX を見直してください。")

    save_cookies_netscape(cookies)


if __name__ == "__main__":
    main()
