import os
import json
import urllib.parse
import re
import requests
from seleniumbase import SB

COOKIES = os.getenv("ICEHOST_COOKIES", "").strip()
if not COOKIES:
    print("DISCOVER_RESULT=NO_COOKIES")
    raise SystemExit

token_value = COOKIES
if "icehostpl_session=" in token_value:
    token_value = token_value.split("icehostpl_session=")[1].split(";")[0]
elif "XSRF-TOKEN=" in token_value:
    token_value = token_value.split("XSRF-TOKEN=")[1].split(";")[0]
token_value = urllib.parse.unquote(token_value.strip())

def send_cookies(br):
    for name in ["icehostpl_session", "XSRF-TOKEN"]:
        br.add_cookie({
            "name": name,
            "value": token_value,
            "domain": "dash.icehost.pl",
            "path": "/",
            "secure": True,
        })

# === A. selenium 睇 freeservers 完整內容 + 撳 SHOW MY SERVERS ===
with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(6)
    send_cookies(sb)
    sb.refresh()
    sb.sleep(6)

    # 撳 SHOW MY SERVERS
    try:
        btn = sb.find_element("text=SHOW MY SERVERS", timeout=5)
        print("SHOW MY SERVERS 按鈕揾到, 撳緊...")
        btn.click()
        sb.sleep(5)
    except Exception as e:
        print("SHOW MY SERVERS:", str(e)[:120])

    print("URL now:", sb.get_current_url())
    body = sb.get_text("body") or ""
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    print(f"=== body 共 {len(lines)} 行, 全部印 ===")
    for l in lines[:150]:
        print("  ", l[:160])

    # 搵數字 pattern / server id
    m = re.findall(r"/servers?/(\d+)", sb.get_page_source())
    print("servers/{id} 出現:", set(m))
    # data-id / data-server 屬性
    for el in sb.find_elements("[data-id]"):
        print("data-id:", el.get_attribute("data-id"), "| text:", (el.text or "")[:60])
    sb.save_screenshot("discover_screenshot.png")

# === B. requests 直打 API 端點(純 HTTP) ===
print("=== B. API 探測 ===")
s = requests.Session()
s.cookies.set("icehostpl_session", token_value, domain="dash.icehost.pl", path="/")
s.cookies.set("XSRF-TOKEN", token_value, domain="dash.icehost.pl", path="/")
s.headers.update({"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"})

for path in ["/api/servers", "/api/client/servers", "/api/user/servers",
             "/api/freeservers", "/api/v1/servers", "/servers.json",
             "/api/servers/list", "/api/me/servers"]:
    try:
        r = s.get("https://dash.icehost.pl" + path, timeout=20)
        ct = r.headers.get("content-type", "")
        snippet = r.text[:200].replace("\n", " ")
        print(f"  GET {path} → {r.status_code} {ct.split(';')[0]} | {snippet}")
    except Exception as e:
        print(f"  GET {path} → ERR {str(e)[:100]}")