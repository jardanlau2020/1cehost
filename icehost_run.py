import json
import os
import time
import urllib.parse
import requests
# 引入 SeleniumBase 高级过盾包
from seleniumbase import SB

SERVER_URL = os.getenv("ICEHOST_SERVER_URL")
ICEHOST_COOKIES = os.getenv("ICEHOST_COOKIES")


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
        return

    # 1. 启动 SeleniumBase 并开启 UC 免密/防检测模式与 Xvfb 虚拟桌面 (xvfb=True)
    with SB(uc=True, xvfb=True) as sb:
        print(f"正在访问 IceHost 面板: {SERVER_URL}")
        # 使用 UC 专属重连模式访问，能极大缓解首屏 Cloudflare 阻断
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
                    print("检测到 JSON 格式 Cookie，正在解析...")

                # 尝试二：如果解析失败，说明填的是纯文本
                except json.JSONDecodeError:
                    print(
                        "检测到纯文本 Cookie 格式，正在自动提取并生成标准字段..."
                    )

                    # 提取真正的 Token 字符串值
                    token_value = raw_cookies_str
                    if "icehostpl_session=" in token_value:
                        token_value = (
                            token_value.split("icehostpl_session=")[1].split(
                                ";"
                            )[0]
                        )
                    elif "XSRF-TOKEN=" in token_value:
                        token_value = token_value.split("XSRF-TOKEN=")[
                            1
                        ].split(";")[0]

                    token_value = token_value.strip()

                    # 自动为 Selenium 生成两个核心的 Cookie 字典
                    cookies_to_add = [
                        {
                            "name": "icehostpl_session",
                            "value": token_value,
                            "domain": "dash.icehost.pl",
                        },
                        {
                            "name": "XSRF-TOKEN",
                            "value": token_value,
                            "domain": "dash.icehost.pl",
                        },
                    ]

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
                print(f"注入 Cookie 过程中发生异常，跳过: {e}")

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
        # 判断是否停留在登录页
        if "login" in current_url or sb.is_element_visible(
            "input[type='email']"
        ):
            msg = (
                "❌ <b>IceHost 登录失效！</b>\n请在浏览器重新提取并更新"
                " ICEHOST_COOKIES。"
            )
            print(msg)
            send_tg_notification(msg, "icehost_debug_screenshot.png")
            return

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

        # 6. 安全寻找并点击续期按钮
        renew_btn_selector = "//*[not(*) and (contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dodaj 6') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add 6'))]"

        try:
            print("正在等待续期按钮加载...")
            sb.wait_for_element_visible(renew_btn_selector, timeout=15)
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

            if is_now_limited:
                msg = (
                    "⚡ <b>IceHost"
                    " 服务器续期成功！</b>\n服务器已真正成功延长 6"
                    " 小时有效期。"
                )
                print(msg)
                send_tg_notification(msg, "icehost_debug_screenshot.png")
            else:
                msg = (
                    "ℹ️ <b>IceHost 续期指令已发送</b>\n按钮已点击，请检查下方截图确认是否成功。"
                )
                print(msg)
                send_tg_notification(msg, "icehost_debug_screenshot.png")

        except Exception as e:
            error_msg = (
                "❌ <b>IceHost"
                " 续期异常！</b>\n未找到续期按钮，可能是网页加载失败、被限制或按钮文本有变，请查看截图。"
            )
            print(f"未在页面中找到可用的续期按钮: {e}")
            send_tg_notification(error_msg, "icehost_debug_screenshot.png")


if __name__ == "__main__":
    run()
