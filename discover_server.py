import os
import json
import urllib.parse
import re
from seleniumbase import SB

COOKIES = os.getenv("ICEHOST_COOKIES", "").strip()
if not COOKIES:
    print("DISCOVER_RESULT=NO_COOKIES")
    raise SystemExit

with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(6)

    token_value = COOKIES
    if "icehostpl_session=" in token_value:
        token_value = token_value.split("icehostpl_session=")[1].split(";")[0]
    elif "XSRF-TOKEN=" in token_value:
        token_value = token_value.split("XSRF-TOKEN=")[1].split(";")[0]
    token_value = token_value.strip()

    for name in ["icehostpl_session", "XSRF-TOKEN"]:
        sb.add_cookie({
            "name": name,
            "value": urllib.parse.unquote(token_value),
            "domain": "dash.icehost.pl",
            "path": "/",
            "secure": True,
        })
    sb.refresh()
    sb.sleep(6)

    def dump_links(tag):
        print(f"=== dump {tag} ===")
        hrefs = set()
        for a in sb.find_elements("a[href]"):
            h = a.get_attribute("href")
            if h and h.startswith("/"):
                hrefs.add("https://dash.icehost.pl" + h)
            elif h and h.startswith("http"):
                hrefs.add(h)
        for h in sorted(hrefs):
            print(f"  A: {h}")
        # buttons / 疑似 server 卡片
        for el in sb.find_elements("button"):
            t = (el.text or "").strip()
            if t:
                print(f"  BTN: {t[:80]}")

    # 訪問 freeservers
    try:
        sb.open("https://dash.icehost.pl/freeservers")
        sb.sleep(6)
        print("FREESERVERS URL:", sb.get_current_url())
        dump_links("freeservers")
        body = sb.get_text("body") or ""
        print("=== freeservers 全文(短) ===")
        for l in body.splitlines()[:60]:
            if l.strip():
                print("  ", l.strip()[:150])
    except Exception as e:
        print("freeservers ERR:", str(e)[:200])

    # 訪問 account
    try:
        sb.open("https://dash.icehost.pl/account")
        sb.sleep(5)
        print("ACCOUNT URL:", sb.get_current_url())
        dump_links("account")
    except Exception as e:
        print("account ERR:", str(e)[:200])

    sb.save_screenshot("discover_screenshot.png")