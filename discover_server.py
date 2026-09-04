import os
import json
import urllib.parse
import re
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

with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(6)
    send_cookies(sb)
    sb.refresh()
    sb.sleep(8)

    # 1) 挖 hidden button ids & 側邊欄 menu 結構
    print("=== hidden button ids ===")
    ids = sb.execute_script("""
      const names = ['console','files','settings','startup','databases','version','backups','splits','schedules','users','subdomain','network'];
      return names.map(n => {
        const el = document.getElementById(n);
        return el ? n + ' => ' + el.tagName + '.' + (el.className||'').toString().slice(0,50) + ' style=' + (el.getAttribute('style')||'') + ' type=' + (el.getAttribute('type')||'') + ' hidden=' + (el.hidden||'') : n + ' => MISSING';
      });
    """)
    for x in ids:
        print("  ", x)

    # 2) 撳側邊欄 menu: Servers 連結
    print("=== 撳 Servers menu ===")
    r = sb.execute_script("""
      const a = Array.from(document.querySelectorAll('a,span,div')).find(x => x.textContent.trim() === 'Servers');
      if (!a) return 'NOT_FOUND';
      a.click(); return 'CLICKED ' + a.tagName + '.' + (a.className||'').toString().slice(0,60);
    """)
    print(r)
    sb.sleep(5)
    print("URL:", sb.get_current_url())

    # 3) 直接試 SPA 路由(server console / settings / freeservers)
    print("=== 試 SPA 路由 ===")
    for path in ["/console", "/files", "/settings", "/server/1", "/servers/1",
                 "/server/1/console", "/freeservers?tab=assigned", "/servers"]:
        try:
            sb.open("https://dash.icehost.pl" + path)
            sb.sleep(3)
            t = sb.get_page_title() or ""
            bodytext = (sb.get_text("body") or "")[:120].replace("\n", " | ")
            print(f"  {path} → title='{t}' body='{bodytext}'")
        except Exception as e:
            print(f"  {path} → ERR {str(e)[:80]}")

    # 4) 挖 network performance entries 睇 API 呼叫
    print("=== performance entries ===")
    perf = sb.execute_script("""
      return performance.getEntriesByType('resource').map(r => r.name).filter(n => !/\.(png|jpg|css|woff|svg|js\\?|fonts)/.test(n) && n.includes('icehost')).slice(-20);
    """)
    for p in perf:
        print("  RES:", p)

    # 5) 直接睇 main JS bundle 有冇 /api/ 字樣
    print("=== 掃描 JS bundles API endpoints ===")
    scripts = sb.execute_script("return Array.from(document.scripts).map(s=>s.src).filter(Boolean);")
    for s in scripts:
        print("  SCRIPT:", s[:120])

    sb.save_screenshot("discover_screenshot.png")