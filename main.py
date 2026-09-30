import json
import os
import re
import time
import urllib.parse
import requests
# 引入 SeleniumBase 高级过盾包
from seleniumbase import SB

SERVER_URL = os.getenv("ICEHOST_SERVER_URL")
ICEHOST_COOKIES = os.getenv("ICEHOST_COOKIES")
ICEHOST_EMAIL = os.getenv("ICEHOST_EMAIL", "")
ICEHOST_PASSWORD = os.getenv("ICEHOST_PASSWORD", "")
ACCOUNT_NAME = os.getenv("ICEHOST_ACCOUNT_NAME", "")
PROXY_SERVER = os.getenv("PROXY_SERVER", "")

SERVICE_NAME = "1cehost"


def now_local():
    """UTC+8 當地時間 MM-DD HH:MM（runner 係 UTC）"""
    return time.strftime("%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))


def fmt_exp(value):
    """到期時間 'YYYY-MM-DD HH:MM' → 'MM-DD HH:MM'（讀唔到就回 '讀唔到'）"""
    if not value:
        return "讀唔到"
    text = str(value)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", text)
    if m:
        return f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(2)}-{m.group(3)}"
    return text[:16]


def build_notice(account, status, detail=None, warn=False, ok=0, skip=0, bad=0):
    """瘦身通知：表頭一行（時間＋統計）＋每項一行；失敗／需人手先加 ⚠️ 一行"""
    lines = [
        f"🎮 {SERVICE_NAME} 續期 ｜ {now_local()} ｜ ✅ {ok} ｜ ⏭️ {skip} ｜ ❌ {bad}",
        " · ".join(
            part for part in (f"▪️ {account or '默認帳號'}", status, detail) if part
        ),
    ]
    if warn:
        lines.append("⚠️ 睇 workflow log 排查")
    return "\n".join(lines)


def send_tg_notification(message, photo_path=None):
    """发送结果和截图至 Telegram，并在发送后清理本地截图文件"""
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        print("未配置 TG 机器人变量，跳过发送 TG 推送。")
        return

    # 1. 发送文本消息
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        requests.post(url, json=payload, timeout=15)
        print("TG 状态通知发送成功。")
    except Exception as e:
        print(f"发送 TG 消息异常: {e}")

    # 2. 发送截图文件并自动清理
    if photo_path and os.path.exists(photo_path):
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": chat_id, "caption": "📸 实时画面"}
                requests.post(url, data=data, files=files, timeout=20)
            print("TG 截图发送成功。")
        except Exception as e:
            print(f"发送 TG 截图异常: {e}")
        finally:
            # 推送完毕后删除本地截图文件，保持环境整洁
            try:
                if os.path.exists(photo_path):
                    os.remove(photo_path)
                    print(f"临时截图文件 {photo_path} 已自动清理。")
            except Exception as e:
                print(f"清理临时截图文件失败: {e}")


def run():
    if not SERVER_URL:
        print("错误: 缺少 ICEHOST_SERVER_URL 环境变量")
        raise SystemExit(2)
    if not ICEHOST_COOKIES and not (ICEHOST_EMAIL and ICEHOST_PASSWORD):
        print("错误: 缺少 ICEHOST_COOKIES,且未配置 ICEHOST_EMAIL/ICEHOST_PASSWORD,无法续期")
        raise SystemExit(2)
    if not ICEHOST_COOKIES:
        print("提示: 无 Cookie,将直接使用账户密码登录。")

    # 1. 启动 SeleniumBase 并开启 UC 免密/防检测模式与 Xvfb 虚拟桌面 (xvfb=True)
    proxy_arg = None
    if PROXY_SERVER:
        print(f"使用代理: {PROXY_SERVER}")
        proxy_arg = PROXY_SERVER
    with SB(uc=True, xvfb=True, proxy=proxy_arg) as sb:
        print(f"正在访问面板: {SERVER_URL}")
        sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=8)
        sb.sleep(5)

        # 2. 注入 Cookies（智能兼容 JSON 或纯文本格式）
        if ICEHOST_COOKIES:
            try:
                cookies_to_add = []
                raw_cookies_str = ICEHOST_COOKIES.strip()

                # 尝试一：如果 Secret 填的是标准的 JSON 格式
                try:
                    raw_data = json.loads(raw_cookies_str)
                    if isinstance(raw_data, list):
                        cookies_to_add = raw_data
                    elif isinstance(raw_data, dict):
                        cookies_to_add = raw_data.get("cookies", [])
                    required = {"icehostpl_session", "XSRF-TOKEN"}
                    present = {str(c.get("name")) for c in cookies_to_add if isinstance(c, dict)}
                    if not required.issubset(present):
                        missing = ", ".join(sorted(required - present))
                        raise ValueError(f"JSON Cookie 缺少: {missing}")
                    print("检测到 JSON 格式 Cookie，正在解析（session/XSRF 已核对）...")

                # 尝试二：如果解析失败，解析 Cookie header / KEY=value 文本
                except json.JSONDecodeError:
                    print(
                        "检测到纯文本 Cookie 格式，正在自动提取并生成标准字段..."
                    )

                    # 支持 `name=value; name2=value2`，避免把 XSRF 值误当 session
                    pairs = {}
                    for part in raw_cookies_str.split(";"):
                        if "=" in part:
                            name, value = part.strip().split("=", 1)
                            pairs[name.strip()] = value.strip()
                    cookies_to_add = [
                        {"name": name, "value": pairs[name], "domain": "dash.icehost.pl"}
                        for name in ("icehostpl_session", "XSRF-TOKEN")
                        if pairs.get(name)
                    ]
                    if not cookies_to_add:
                        raise ValueError("纯文本 Cookie 中未找到 icehostpl_session/XSRF-TOKEN")

                # 统一执行转换与注入
                for c in cookies_to_add:
                    raw_value = c["value"]
                    decoded_value = urllib.parse.unquote(raw_value)

                    cookie_dict = {
                        "name": c["name"],
                        "value": decoded_value,
                        "domain": c.get("domain", "dash.icehost.pl"),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", True),
                    }
                    if "sameSite" in c:
                        ss = str(c["sameSite"]).lower()
                        if ss in ["lax", "strict", "none"]:
                            cookie_dict["sameSite"] = ss.capitalize()

                    sb.add_cookie(cookie_dict)

                print("Cookie 成功注入！")

                # 重新刷新加载，应用 Cookie
                sb.refresh()
                sb.sleep(5)
            except Exception as e:
                print(f"注入 Cookie 过程中发生异常: {e}")
                raise SystemExit(2)

        # 3. 核心过盾：自动寻找并执行系统级物理点击过 Cloudflare Turnstile 验证盾
        sb.save_screenshot("run_screenshot.png")
        try:
            print(
                "正在检测并调用系统级 PyAutoGUI 驱动，物理点击 Cloudflare"
                " 人机验证码..."
            )
            # 在虚拟桌面上定位验证框并模拟发送系统硬件级点击事件
            sb.uc_gui_click_captcha()
            sb.sleep(10)  # 给予 10 秒跳转缓冲
            sb.save_screenshot("run_screenshot.png")
        except Exception as e:
            print(f"验证盾已被跳过或点击执行完毕: {e}")

        # 4. 判断登录状态
        current_url = sb.get_current_url()
        page_src_login = sb.get_page_source()
        # 多重判据：URL、登入表单、页面文字；任一命中即视为 cookie 失效
        login_markers = [
            "Zaloguj", "Logowanie", "Sign in", "Log in",
            "Remember me", "Zapamiętaj mnie", "Forgot password",
        ]
        cookie_dead = (
            "login" in current_url
            or sb.is_element_visible("input[type='email']")
            or sb.is_element_visible("input[type='password']")
            or any(m in page_src_login for m in login_markers)
        )
        if cookie_dead:
            # 新增:Cookie 失效時,若已配置賬戶密碼,自動轉密碼登入
            if ICEHOST_EMAIL and ICEHOST_PASSWORD:
                print("Cookie 失效,嘗試用賬戶密碼登入...")
                # 關鍵修(六輪實測): WAF 只驗 session cookie「存在」唔驗「有效」
                # (Run 34151573065: 死 session 都照渲染表單), 一旦冇 session
                # cookie 就 "WAF - Block"。所以:
                #   1. 保留 icehostpl_session(死值都係 WAF 放行牌)
                #   2. 只刪 XSRF-TOKEN(死 token 係 "CSRF mismatch" 元兇)
                #   3. refresh 後後端派新 session+新 XSRF, 登入 XHR 就能過 CSRF
                try:
                    for c in sb.driver.get_cookies():
                        if c.get("name") == "XSRF-TOKEN":
                            try:
                                sb.driver.delete_cookie(c["name"])
                            except Exception:
                                pass
                    print("已移除 XSRF-TOKEN(保留 session cookie 作 WAF 放行牌)。")
                except Exception as e:
                    print(f"移除 XSRF cookie 異常: {e}")
                # 用原頁 refresh 而唔係新導航: 新導航 /auth/login 會被 WAF 判做
                # 新訪客直接 "WAF - Block"(Run 34152836486 實測), refresh 有機會過
                try:
                    sb.refresh()
                    sb.sleep(10)
                except Exception as e:
                    print(f"refresh 異常: {e}")
                # 確認登入表單真係渲染咗,否則 dump 頁面狀態即失敗(唔好喺空页面盲填)
                pw_visible = False
                for _attempt in range(3):
                    try:
                        sb.wait_for_element_visible("input[type='password']", timeout=10)
                        pw_visible = True
                        break
                    except Exception:
                        sb.sleep(5)
                if not pw_visible:
                    _url = sb.get_current_url()
                    _src = sb.get_page_source()
                    print(f"登入表單未顯示。URL: {_url}")
                    print("=== PAGE SOURCE DUMP ===")
                    print(_src[:2500])
                    print("=== END PAGE SOURCE ===")
                    sb.save_screenshot("run_screenshot.png")
                    send_tg_notification(
                        build_notice(
                            ACCOUNT_NAME,
                            "❌ 登入頁異常:表單冇渲染（CF 盾／頁面結構變動）",
                            warn=True, bad=1),
                        "run_screenshot.png")
                    raise SystemExit(3)
                try:
                    sb.uc_gui_click_captcha()
                    sb.sleep(10)
                except Exception as e:
                    print(f"登入頁驗證盾處理異常(可忽略): {e}")
                try:
                    # 實測 DOM: email 欄 = input[name='username'](type=text), 其餘 fallback
                    email_locators = [
                        "input[name='username']",
                        "input[type='text']",
                        "input[name='email']",
                        "input[type='email']",
                    ]
                    filled_email = False
                    for loc in email_locators:
                        try:
                            sb.update_text(loc, ICEHOST_EMAIL)
                            print(f"email 欄已填({loc})。")
                            filled_email = True
                            break
                        except Exception:
                            continue
                    if not filled_email:
                        # 通用 fallback: 搵第一個可见嘅非 password/checkbox/radio/hidden input
                        try:
                            print("通用 fallback: 列出所有 input...")
                            for el in sb.find_elements("input"):
                                _t = (el.get_attribute("type") or "text").lower()
                                if _t in ("password", "checkbox", "radio", "hidden", "submit", "button", "file"):
                                    continue
                                _nm = (el.get_attribute("name") or "")
                                _ph = (el.get_attribute("placeholder") or "")
                                print(f"  候選 input: name={_nm!r} type={_t} placeholder={_ph!r}")
                                sb.update_text(el, ICEHOST_EMAIL)
                                print(f"已填 email 入通用 input(name={_nm!r})。")
                                filled_email = True
                                break
                        except Exception as _e2:
                            print(f"通用 fallback 失敗: {_e2}")
                    if not filled_email:
                        print("⚠️ 找不到 email 輸入欄,僅填密碼(可能失敗)。")
                    sb.update_text("input[type='password']", ICEHOST_PASSWORD)
                    sb.sleep(2)
                    try:
                        sb.click('button[type="submit"]')
                    except Exception:
                        # 可能係 input[type=submit] 或者要 Enter
                        try:
                            sb.click('input[type="submit"]')
                        except Exception:
                            sb.press_keys('input[type="password"]', '\n')
                    print("已提交登入表單,等待跳轉...")
                    sb.sleep(15)
                    sb.save_screenshot("run_screenshot.png")

                    # 登入後再次判定是否仍停留在登入頁
                    cur = sb.get_current_url()
                    src = sb.get_page_source()
                    still_login = (
                        "login" in cur
                        or sb.is_element_visible("input[type='password']")
                        or any(m in src for m in login_markers)
                    )
                    if not still_login:
                        print("✅ 密碼登入成功,繼續執行續期流程。")
                        sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=8)
                        sb.sleep(5)
                        # 登入成功,跳過 exit,繼續往下續期
                    else:
                        # 失敗時 dump 頁面錯誤訊息(紅字/提示),方便定位
                        try:
                            import re as _re3
                            _err_block = _re3.findall(
                                r'(?:alert|error|invalid|invalid-feedback|text-danger|danger|warning|form-text|message)[^>]*>([^<]{4,120})',
                                src, _re3.I)
                            print(f"登入失敗後 URL: {cur}")
                            print(f"頁面錯誤提示: {_err_block[:10]}")
                        except Exception as _e3:
                            print(f"錯誤 dump 失敗: {_e3}")
                        msg = build_notice(
                            ACCOUNT_NAME,
                            "❌ 密碼登入後仍在登入頁,查 EMAIL/PASSWORD 或人機驗證",
                            warn=True, bad=1)
                        print("密碼登入失敗,已發 TG 通知。")
                        send_tg_notification(msg, "run_screenshot.png")
                        raise SystemExit(3)
                except SystemExit:
                    raise
                except Exception as e:
                    print(f"密碼登入過程異常: {e}")
                    raise SystemExit(3)
            else:
                msg = build_notice(
                    ACCOUNT_NAME,
                    "❌ Cookie 失效需人手換:F12 抄 icehostpl_session 更新 ICEHOST_COOKIES",
                    warn=True, bad=1)
                print("❌ Cookie 已失效,已發 TG 通知要求更換。")
                send_tg_notification(msg, "run_screenshot.png")
                # Cookie 失效係真正失敗,回傳非零,避免 Matrix workflow 假綠燈
                raise SystemExit(2)
        print("✅ Cookie 有效,登入狀態正常。" )

        # 5. 判定波兰语与英语红框限制
        page_source = sb.get_page_source()
        keywords = [
            "Nie możesz przedłużyć",
            "niedawno to zrobiłeś",
            "kolejne 6 godziny",
            "cannot extend",
            "recently",
            "next 6 hours",
        ]
        is_limited = any(kw in page_source for kw in keywords)

        if is_limited:
            print(
                "检测到红框限制提示：说明未到可续期时间。结束本次运行（不发送"
                " Telegram 提醒）。"
            )
            return

        # 6. 安全寻找并点击续期按钮（兼容 ADD 6 HOURS VALIDITY / Dodaj 6 godzin / add 6）
        renew_btn_selector = "//*[not(*) and (contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add 6 hours') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dodaj 6') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add 6'))]"

        try:
            print("正在等待续期按钮加载...")
            sb.wait_for_element_visible(renew_btn_selector, timeout=15)

            # 读取续期前到期时间（EXPIRATION DATE），供续期后对比
            import re as _re
            _src0 = sb.get_page_source()
            _m0 = _re.search(r'(EXPIRATION DATE|Data wygaśnięcia|到期日|expiry)[^0-9]{0,40}([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2})', _src0, _re.I | _re.S)
            _old_exp = _m0.group(2) if _m0 else None
            print(f"续期前 EXPIRATION DATE: {_old_exp}")

            print("未检测到限制提示，找到续期按钮，正在点击...")
            sb.click(renew_btn_selector)

            # ⚡ 点击后，在不刷新页面的前提下，先等待 5 秒让可能弹出的红框提示充分渲染
            sb.sleep(5)
            sb.save_screenshot("run_screenshot.png")

            # 立即读取当前最真实的页面源码（此时若有报错红条，必定还挂在屏幕上）
            current_source = sb.get_page_source()
            is_failed_due_to_limit = any(
                kw in current_source for kw in keywords
            )

            if is_failed_due_to_limit:
                print(
                    "点击后，页面立刻弹出了限制提示：说明未到可续期时间。结束本次运行。"
                )
                return

            print("点击后未检测到报错红条，正在刷新页面确认续期结果...")
            sb.refresh()
            sb.sleep(5)
            sb.save_screenshot("run_screenshot.png")

            updated_source = sb.get_page_source()
            is_now_limited = any(kw in updated_source for kw in keywords)

            # 读取续期后到期时间并对比，确认真正延长约6小时
            _m1 = _re.search(r'(EXPIRATION DATE|Data wygaśnięcia|到期日|expiry)[^0-9]{0,40}([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2})', updated_source, _re.I | _re.S)
            _new_exp = _m1.group(2) if _m1 else None
            print(f"续期后 EXPIRATION DATE: {_new_exp}")

            from datetime import datetime as _dt
            def _parse_exp(s):
                if not s:
                    return None
                s = s.replace('T', ' ')
                for _fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                    try:
                        return _dt.strptime(s, _fmt)
                    except Exception:
                        continue
                return None

            _confirmed = False
            if _old_exp and _new_exp:
                _ot = _parse_exp(_old_exp)
                _nt = _parse_exp(_new_exp)
                if _ot and _nt:
                    _d = (_nt - _ot).total_seconds()
                    # 正常 +6 小时；允许 5~9 小时容差(18000~32400 秒)
                    if 18000 <= _d <= 32400:
                        _confirmed = True

            if _confirmed:
                msg = build_notice(
                    ACCOUNT_NAME,
                    f"✅ 已續期 → {fmt_exp(_new_exp)}",
                    f"前 {fmt_exp(_old_exp)}",
                    ok=1)
                print(msg)
                send_tg_notification(msg, "run_screenshot.png")
            elif is_now_limited:
                print(
                    "刷新后检测到限制提示：说明未到可续期时间。本次未完成续期。"
                )
            elif _old_exp and _new_exp and _old_exp == _new_exp:
                msg = build_notice(
                    ACCOUNT_NAME,
                    "⏭️ 未可續（到期時間冇變）",
                    f"到期 {fmt_exp(_new_exp)}",
                    warn=True, skip=1)
                print(msg)
                send_tg_notification(msg, "run_screenshot.png")
            else:
                msg = build_notice(
                    ACCOUNT_NAME,
                    "⏭️ 未可續（未能確認結果）",
                    f"{fmt_exp(_old_exp)} → {fmt_exp(_new_exp)}",
                    warn=True, skip=1)
                print(msg)
                send_tg_notification(msg, "run_screenshot.png")

        except Exception as e:
            print(f"未在页面中找到可用的续期按钮: {e}")
            # [DIAG] 搵唔到掣唔好淨係報錯——dump 全現場俾 log 分析
            try:
                _diag_url = sb.get_current_url()
                print(f"[DIAG] 當前 URL: {_diag_url}")
                # 列出頁面所有按鈕/鏈結文字,搵下掣去咗邊
                _btns = sb.find_elements("button, a.btn, input[type=submit], a[href*='renew'], a[href*='extend']")
                print(f"[DIAG] 頁面共 {len(_btns)} 個按鈕/鏈結候選:")
                for _b in _btns[:40]:
                    try:
                        _txt = (_b.text or _b.get_attribute("value") or "").strip().replace("\n", " ")[:60]
                        _tag = _b.tag_name
                        _href = _b.get_attribute("href") or ""
                        print(f"[DIAG]   <{_tag}> '{_txt}' href={_href[:80]}")
                    except Exception:
                        pass
                # dump 頁面可見文字摘要(去標籤後前 1200 字),睇下過期版頁面係乜樣
                import re as _rd
                _vis = _rd.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', sb.get_page_source(), flags=_rd.S | _rd.I)
                _vis = _rd.sub(r'<[^>]+>', ' ', _vis)
                _vis = _rd.sub(r'\s+', ' ', _vis)
                print(f"[DIAG] 頁面可見文字(前1200字): {_vis[:1200]}")
                # [DIAG2] suspended 頁偵測: 全量 <a href>(di scan 漏普通 link)+ banner 區 HTML
                try:
                    _links = sb.find_elements("a[href]")
                    print(f"[DIAG2] 全頁 <a href> 共 {len(_links)}:")
                    for _a in _links[:60]:
                        try:
                            _t2 = (_a.text or "").strip().replace("\n", " ")[:60]
                            _h2 = _a.get_attribute("href") or ""
                            if _t2 or any(k in _h2 for k in ("renew", "extend", "pay", "opla", "przed", "suspend", "react", "servers")):
                                print(f"[DIAG2]   <a> '{_t2}' href={_h2[:100]}")
                        except Exception:
                            pass
                    _src_raw = sb.get_page_source()
                    _mi = _src_raw.find("Suspended")
                    if _mi >= 0:
                        _seg = _src_raw[max(0, _mi - 600):_mi + 900]
                        print(f"[DIAG2] Suspended 區域 HTML:\n{_seg}")
                    else:
                        print("[DIAG2] 頁面無 'Suspended' 字眼")
                except Exception as _de2:
                    print(f"[DIAG2] dump 失敗: {_de2}")
            except Exception as _de:
                print(f"[DIAG] dump 失敗: {_de}")
            sb.save_screenshot("run_screenshot.png")
            _issusp = "Suspended" in sb.get_page_source()
            if _issusp:
                error_msg = build_notice(
                    ACCOUNT_NAME,
                    "❌ 已被停權（Suspended）,續期掣消失,需人手入 panel 解封",
                    "72h 寬限期後刪機",
                    warn=True, bad=1)
            else:
                error_msg = build_notice(
                    ACCOUNT_NAME,
                    "❌ 搵唔到續期掣（ADD 6 HOURS）,可能 WAF 擋／未到窗口",
                    warn=True, bad=1)
            send_tg_notification(error_msg, "run_screenshot.png")
            raise SystemExit(1)


if __name__ == "__main__":
    run()
