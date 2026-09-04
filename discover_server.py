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

    # 1) 挖 JS 函式源碼 — 睇 server URL pattern
    print("=== JS 函式源碼 ===")
    for fn in ["openConsole", "openFiles", "openSettings", "openConsole2", "Server"]:
        try:
            src = sb.execute_script(f"return typeof {fn} !== 'undefined' ? {fn}.toString().slice(0,600) : 'UNDEFINED';")
            print(f"[{fn}] {src[:400]}")
        except Exception as e:
            print(f"[{fn}] ERR {str(e)[:80]}")

    # 2) 搵 global 有冇 server id
    print("=== global server 變數 ===")
    g = sb.execute_script("""
      const out = [];
      for (const k of Object.keys(window)) {
        if (/server|serv|id|panel|console/i.test(k) && k.length < 60) {
          let v;
          try { v = window[k]; } catch(e) { v = 'ERR'; }
          if (v !== null && v !== undefined) {
            let s = typeof v === 'string' ? v : JSON.stringify(v)?.slice(0,80);
            if (s && String(s).length < 120) out.push(k + ' = ' + s);
          }
        }
      }
      return out.slice(0,40);
    """)
    for line in g:
        print("  ", line)

    # 3) 撳 SHOW ASSIGNED SERVERS tab(JS 精準)
    print("=== 撳 SHOW ASSIGNED SERVERS ===")
    clicked = sb.execute_script("""
      const nodes = Array.from(document.querySelectorAll('*'));
      const el = nodes.find(n => n.childNodes.length && Array.from(n.childNodes).some(c => c.nodeType===3 && c.textContent.trim()==='SHOW ASSIGNED SERVERS'));
      if (!el) return 'NOT_FOUND';
      el.click(); return 'CLICKED ' + el.tagName + '.' + (el.className||'').toString().slice(0,50);
    """)
    print(clicked)
    sb.sleep(5)
    print("URL:", sb.get_current_url())

    # 4) same-origin fetch 打 API(瀏覽器內,帶 cookie,WAF 已過)
    print("=== in-page API fetch ===")
    api_results = sb.execute_script("""
      const paths = ['/api/servers','/api/user/servers','/api/client/servers','/api/freeservers','/api/v1/servers','/freeservers/list','/api/servers/list'];
      return Promise.all(paths.map(async p => {
        try {
          const r = await fetch(p, {headers: {'X-Requested-With':'XMLHttpRequest', 'Accept':'application/json'}});
          const t = await r.text();
          return p + ' → ' + r.status + ' | ' + t.slice(0,300).replace(/\\n/g,' ');
        } catch(e) { return p + ' → ERR ' + String(e).slice(0,100); }
      }));
    """)
    for line in api_results:
        print("  ", line[:350])

    # 5) 挖 HTML 有冇 hidden server data / JSON state
    print("=== 頁面內嵌 JSON/state ===")
    for m in re.finditer(r'window\.__[A-Z_]+\s*=\s*([^<]{0,200})', sb.get_page_source()):
        print("  __", m.group(0)[:220])

    sb.save_screenshot("discover_screenshot.png")