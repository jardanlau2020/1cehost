import json
import os
import time
import urllib.parse
import requests
# 引入 SeleniumBase 高级过盾包
from seleniumbase import SB

SERVER_URL = os.getenv("ICEHOST_SERVER_URL")
ICEHOST_COOKIES = os.getenv("ICEHOST_COOKIES")
ICEHOST_EMAIL = os.getenv("ICEHOST_EMAIL", "")
ICEHOST_PASSWORD = os.getenv("ICEHOST_PASSWORD", "")
ACCOUNT_NAME = os.getenv("ICEHOST_ACCOUNT_NAME", "IceHost")
PROXY_SERVER = os.getenv("PROXY_SERVER", "")


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
                data = {"chat_id": chat_id, "caption": "📸 IceHost 实时画面"}
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
        print(f"正在访问 IceHost 面板: {SERVER_URL}")
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
        sb.save_screenshot("icehost_debug_screenshot.png")
        try:
            print(
                "正在检测并调用系统级 PyAutoGUI 驱动，物理点击 Cloudflare"
                " 人机验证码..."
            )
            # 在虚拟桌面上定位验证框并模拟发送系统硬件级点击事件
            sb.uc_gui_click_captcha()
            sb.sleep(10)  # 给予 10 秒跳转缓冲
            sb.save_screenshot("icehost_debug_screenshot.png")
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
                login_url = "https://dash.icehost.pl/auth/login"
                sb.uc_open_with_reconnect(login_url, reconnect_time=8)
                sb.sleep(5)
                try:
                    sb.uc_gui_click_captcha()
                    sb.sleep(10)
                except Exception as e:
                    print(f"登入頁驗證盾處理異常(可忽略): {e}")
                try:
                    sb.update_text("input[type='email']", ICEHOST_EMAIL)
                    sb.update_text("input[type='password']", ICEHOST_PASSWORD)
                    sb.sleep(2)
                    sb.click('button[type="submit"]')
                    print("已提交登入表單,等待跳轉...")
                    sb.sleep(15)
                    sb.save_screenshot("icehost_debug_screenshot.png")

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
                        msg = (
                            f"❌ <b>{ACCOUNT_NAME} 密碼登入失敗</b>\n\n"
                            f"已嘗試自動登入但仍在登入頁,請檢查 <code>ICEHOST_EMAIL</code>/<code>ICEHOST_PASSWORD</code> 是否正確,或登入頁有人機驗證無法通過。\n\n"
                            f"⏰ 偵測時間: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"🔗 當前 URL: {cur[:80]}"
                        )
                        print("密碼登入失敗,已發 TG 通知。")
                        send_tg_notification(msg, "icehost_debug_screenshot.png")
                        raise SystemExit(3)
                except SystemExit:
                    raise
                except Exception as e:
                    print(f"密碼登入過程異常: {e}")
                    raise SystemExit(3)
            else:
                msg = (
                    f"🔁 <b>{ACCOUNT_NAME} Cookie 已失效,需要更換!</b>\n\n"
                    "自動續期已停止,因為 <code>icehostpl_session</code> 過期或被後台踢出。\n\n"
                    "<b>請照做:</b>\n"
                    "1. 用瀏覽器登入 dash.icehost.pl\n"
                    "2. F12 → Application → Cookies → 複製 <code>icehostpl_session</code> 全值\n"
                    "3. 貼返俾助手更新 <code>ICEHOST_COOKIES</code>\n\n"
                    f"⏰ 偵測時間: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"🔗 當前 URL: {current_url[:80]}"
                )
                print("❌ Cookie 已失效,已發 TG 通知要求更換。")
                send_tg_notification(msg, "icehost_debug_screenshot.png")
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
            sb.save_screenshot("icehost_debug_screenshot.png")

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
            sb.save_screenshot("icehost_debug_screenshot.png")

            updated_source = sb.get_page_source()
            is_now_limited = any(kw in updated_source for kw in keywords)

            # 读取续期后到期时间并对比，确认真正延长约6小时
            _m1 = _re.search(r'(EXPIRATION DATE|Data wygaśnięcia|到期日|expiry)[^0-9]{0,40}([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2})', updated_source, _re.I | _re.S)
            _new_exp = _m1.group(2) if _m1 else None
            print(f"续期后 EXPIRATION DATE: {_new_exp}")

            def _parse_ts(s):
                try:
                    return int(_re.sub(r'[^0-9]', '', s))
                except Exception:
                    return None

            _confirmed = False
            if _old_exp and _new_exp:
                _d = _parse_ts(_new_exp) - _parse_ts(_old_exp)
                # 差约 5~7 小时 = 成功（正常加6小时）；允许 5~9 小时容差
                if _d is not None and 5000 <= _d <= 60000:
                    _confirmed = True

            if _confirmed:
                msg = (
                    f"⚡ <b>{ACCOUNT_NAME} 續期成功!</b>\n\n"
                    f"到期時間: {_old_exp} → <b>{_new_exp}</b>(+6 小時)\n"
                    f"⏰ 執行時間: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print(msg)
                send_tg_notification(msg, "icehost_debug_screenshot.png")
            elif is_now_limited:
                print(
                    "刷新后检测到限制提示：说明未到可续期时间。本次未完成续期。"
                )
            elif _old_exp and _new_exp and _old_exp == _new_exp:
                msg = (
                    f"⚠️ <b>{ACCOUNT_NAME} 續期未生效</b>\n\n"
                    f"已撳過續期掣,但到期時間冇變({_new_exp})。\n"
                    f"可能未到可續期窗口,或後端拒絕。請睇截圖。\n"
                    f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print(msg)
                send_tg_notification(msg, "icehost_debug_screenshot.png")
            else:
                msg = (
                    f"ℹ️ <b>{ACCOUNT_NAME} 續期指令已發送</b>\n\n"
                    f"續期前: {_old_exp or '讀唔到'}\n"
                    f"續期後: {_new_exp or '讀唔到'}\n"
                    f"未能完成時間對比,請睇截圖確認。\n"
                    f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print(msg)
                send_tg_notification(msg, "icehost_debug_screenshot.png")

        except Exception as e:
            error_msg = (
                f"❌ <b>{ACCOUNT_NAME} 續期異常!</b>\n\n"
                f"搵唔到續期掣(ADD 6 HOURS VALIDITY),可能原因:\n"
                f"• 網頁載入失敗或被 WAF 擋\n"
                f"• 未到可續期窗口(掣被隱藏)\n"
                f"• 掣文字有變\n\n"
                f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print(f"未在页面中找到可用的续期按钮: {e}")
            send_tg_notification(error_msg, "icehost_debug_screenshot.png")
            raise SystemExit(1)


if __name__ == "__main__":
    run()
