import os
import json
import urllib.parse
from seleniumbase import SB

COOKIES = os.getenv("ICEHOST_COOKIES", "").strip()
if not COOKIES:
    print("DISCOVER_RESULT=NO_COOKIES")
    raise SystemExit

with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(5)

    # 注入 cookie(同 icehost_run.py 一樣嘅純文本邏輯)
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
    sb.sleep(5)

    # 所有指向 /servers/ 嘅連結(edit 頁通常有 id)
    links = set()
    for a in sb.find_elements("a[href*='/servers/']"):
        href = a.get_attribute("href")
        if href:
            links.add(href)
    for href in sorted(links):
        print("LINK:", href)

    # 頁面標題 + 當前 URL,判斷登入狀態
    print("TITLE:", sb.get_page_title() or "(none)")
    print("URL:", sb.get_current_url())

    # 若只搵到一個 server link 就 print 結果
    edit_urls = [l for l in links if "/edit" in l or "/servers/" in l]
    if len(edit_urls) == 1:
        print(f"DISCOVER_RESULT={edit_urls[0]}")
    else:
        print(f"DISCOVER_RESULT=MULTIPLE_OR_NONE ({len(edit_urls)})")

    sb.save_screenshot("discover_screenshot.png")