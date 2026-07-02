#!/usr/bin/env python3
"""
klima-monitor — a personal stock monitor for the Midea PortaSplit (or any product).

How it works (same idea as braucheklima.de, just for your own stores):
    1. POLL   — every N minutes, ask each retailer whether the product is in stock.
    2. DIFF   — compare against the status saved from the last check.
    3. NOTIFY — only when something flips from "unavailable" -> "available",
                send a Telegram message (and/or email).

You configure WHAT to watch in config.yaml. Each "target" describes one
product-at-one-store and how to read its availability. Two check modes:

    mode: "json"  -> call a JSON API endpoint (best; how the real retailers work)
    mode: "html"  -> fetch a page and look for a text marker (fallback)

See README.md for how to find the real OBI/Bauhaus/etc. endpoint with your
browser's DevTools. That is the key step.
"""

import json
import os
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

import requests

try:
    import yaml
except ImportError:
    print("Missing dependency. Run:  pip install pyyaml requests")
    sys.exit(1)

STATE_FILE = Path(__file__).parent / "state.json"
CONFIG_FILE = Path(__file__).parent / "config.yaml"

# A normal browser-looking header set. Be polite: don't poll faster than needed.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


# --------------------------------------------------------------------------- #
# State handling
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Availability checks
# --------------------------------------------------------------------------- #
def check_json(target: dict) -> bool:
    """
    Call a JSON endpoint and decide availability from a field.

    config keys:
        url              : the endpoint (find it via DevTools -> Network)
        available_path   : dotted path into the JSON, e.g. "data.0.stock.available"
        available_equals : the value that means "in stock" (default: True)
        method / params / headers / json_body : optional
    """
    method = target.get("method", "GET").upper()
    headers = {**DEFAULT_HEADERS, **target.get("headers", {})}
    resp = requests.request(
        method,
        target["url"],
        params=target.get("params"),
        json=target.get("json_body"),
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    # Walk the dotted path (supports list indices too).
    value = data
    for key in str(target["available_path"]).split("."):
        if isinstance(value, list):
            value = value[int(key)]
        else:
            value = value[key]

    expected = target.get("available_equals", True)
    # Compare loosely so "true"/"True"/1 all work.
    return str(value).strip().lower() == str(expected).strip().lower()


def check_html(target: dict) -> bool:
    """
    Fetch a page and decide availability from text markers.

    config keys:
        url                  : the product/store page
        unavailable_markers  : list of texts that appear ONLY when out of stock.
                               If ANY of them is on the page -> not available.
                               (This is the reliable approach for MediaMarkt:
                               watch for the "out of stock" banner to disappear.)
        unavailable_marker   : single-string version of the above (optional)
        available_marker     : text present ONLY when in stock (optional).
                               If given, it must ALSO be present to count as
                               available. Use with care — some phrases like
                               "in den warenkorb" leak from unrelated sections
                               of the page (e.g. suggested products), so a bare
                               positive marker can cause false alarms.
    """
    headers = {**DEFAULT_HEADERS, **target.get("headers", {})}
    resp = requests.get(target["url"], headers=headers, timeout=20)
    resp.raise_for_status()
    html = resp.text.lower()

    # SAFETY: make sure we actually got the real product page and not a
    # bot-block / CAPTCHA page. If this marker (e.g. the article number) is
    # missing, we were probably blocked -> raise instead of guessing, so we
    # never send a false "available" alert.
    page_valid = target.get("page_valid_marker")
    if page_valid and page_valid.lower() not in html:
        raise RuntimeError(
            "page did not contain the expected product marker "
            f"'{page_valid}' — likely blocked or wrong page; skipping this check"
        )

    # Gather out-of-stock markers (accept a list or a single string).
    unavail = target.get("unavailable_markers") or target.get("unavailable_marker")
    if isinstance(unavail, str):
        unavail = [unavail]

    avail = target.get("available_marker")

    if unavail:
        # If any out-of-stock banner is present, it's not available.
        for marker in unavail:
            if marker.lower() in html:
                return False
        # No out-of-stock banner found. If a positive marker is also
        # required, check it too; otherwise assume it's available.
        if avail:
            return avail.lower() in html
        return True

    # No unavailable markers configured: fall back to the positive marker.
    if avail:
        return avail.lower() in html
    return False


def is_available(target: dict) -> bool:
    mode = target.get("mode", "json")
    if mode == "json":
        return check_json(target)
    if mode == "html":
        return check_html(target)
    raise ValueError(f"Unknown mode '{mode}' for target '{target.get('name')}'")


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def notify_telegram(cfg: dict, text: str) -> None:
    token = cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = cfg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  [telegram] skipped (no bot_token / chat_id configured)")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )
    if r.ok:
        print("  [telegram] sent")
    else:
        print(f"  [telegram] FAILED: {r.status_code} {r.text[:200]}")


def notify_email(cfg: dict, subject: str, text: str) -> None:
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg.set_content(text)

    with smtplib.SMTP(cfg["smtp_host"], cfg.get("smtp_port", 587)) as s:
        s.starttls()
        s.login(cfg["username"], cfg["password"])
        s.send_message(msg)
    print("  [email] sent")


def send_alert(config: dict, target: dict) -> None:
    name = target["name"]
    url = target.get("link") or target.get("url", "")
    text = f"✅ IN STOCK: {name}\n{url}\n\n{datetime.now():%Y-%m-%d %H:%M}"
    subject = f"In stock: {name}"

    notif = config.get("notifications", {})
    if notif.get("telegram", {}).get("enabled"):
        notify_telegram(notif["telegram"], text)
    if notif.get("email", {}).get("enabled"):
        try:
            notify_email(notif["email"], subject, text)
        except Exception as e:  # noqa: BLE001
            print(f"  [email] FAILED: {e}")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def run_once(config: dict, state: dict) -> dict:
    for target in config["targets"]:
        name = target["name"]
        try:
            available = is_available(target)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] check error: {e}")
            continue

        was_available = state.get(name, False)
        status = "AVAILABLE" if available else "not available"
        print(f"[{datetime.now():%H:%M:%S}] {name}: {status}")

        # Fire only on the transition into availability.
        if available and not was_available:
            print(f"  -> transition to available! sending alerts...")
            send_alert(config, target)

        state[name] = available
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal stock monitor")
    parser.add_argument("--once", action="store_true",
                        help="run a single check and exit (good for cron)")
    parser.add_argument("--config", default=str(CONFIG_FILE))
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text())
    interval = int(config.get("interval_minutes", 5)) * 60

    state = load_state()
    print(f"Loaded {len(config['targets'])} target(s). "
          f"Interval: {interval // 60} min. Mode: "
          f"{'single run' if args.once else 'loop'}\n")

    if args.once:
        state = run_once(config, state)
        save_state(state)
        return

    while True:
        state = run_once(config, state)
        save_state(state)
        print(f"--- sleeping {interval // 60} min ---\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
