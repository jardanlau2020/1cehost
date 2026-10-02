# 1cehost

1cehost（`dash.icehost.pl`）免费服务器的自动续期 —— 定时在面板上点一次
「ADD 6 HOURS VALIDITY」，把到期时间往后推 6 小时。

面板前面有 WAF + Cloudflare，所以用真实浏览器（SeleniumBase 的 UC 模式 +
xvfb 虚拟桌面）发系统级点击过盾，而不是裸 HTTP 请求。

## 它是怎么工作的

`.github/workflows/direct-renew.yml` 每小时（`0 * * * *`）跑一次，对两个账号
各起一个 job：

1. `actions/checkout` 拉代码
2. `scripts/setup_proxy.sh` 尽力挂一个 sing-box 代理换出口（失败就退回直连）
3. `main.py` 走完整个续期流程

`main.py` 的顺序是：

```
打开面板 → 识别 WAF/CF 拦截 → 注入 Cookie → 物理点击过 CF 盾
        → 判定登录态（失效则用账号密码兜底登录）
        → 读续期前到期时间 → 点续期按钮 → 读续期后到期时间
        → 差值是 +5~9 小时才算真续上
```

> **renew-kit 钉在 tag 上**：`jardanlau2020/renew-kit/.github/actions/renew@v0.4.2`，
> 同时 `renewkit-ref: v0.4.2`。不跟 `@main` 是有教训的 —— main 上出过一次
> composite action 的语法错（空 `then` 分支），所有跟 `@main` 的仓库一起变红。
> 升级就改这两个 `v0.4.2`。

## 为什么「未到窗口」要靠点击后的红框判断

实测（run #237 / #239）：**窗口没开时，续期按钮照样在页面上**。真正的信号是
点击之后面板弹的那条红框。所以：

- 点击**前**只用语义明确的强特征把关（`nie możesz przedłużyć` 等）；
- 点击**后**才用宽松特征（`recently`、`next 6 hours`）作佐证。

方向是刻意选的：漏判只是多点一次，面板会再弹红框，无害；误判会让脚本永远不点，
服务器到期被 suspend，**不可逆**。

同理，到期时间读不到时**照点**，事后如实上报 `UNKNOWN`。按钮在不在不是窗口信号，
把「读不到到期时间」当「窗口没开」会直接漏点。

> 这条取舍与 aclclouds 相反：那边按钮语义模糊，读不到剩余天数就不点；
> 这边按钮语义明确，不点才是错。

## 结果语义与退出码

统一走 [renew-kit](https://github.com/jardanlau2020/renew-kit)：

| 结果 | 含义 | 退出码 | 发 TG |
|---|---|---|---|
| `RENEWED` | 到期时间确实 +6h | 0 | ✅ |
| `SKIPPED` | 未到续期窗口 / dry-run | 0 | ❌ 静默 |
| `TRANSIENT` | 上游故障（CF 挑战页、5xx、超时） | 0 | ❌ 静默 |
| `UNKNOWN` | 点了但读不到明确结果 | 0 | ✅ |
| `FAILED` | 真失败，需人工（WAF 拦截 / Cookie+密码都失效 / 按钮消失 / 被停权） | 1 | ✅ |

**跳过时静默是有意的**：本仓每小时跑一次，窗口没开是常态，每次都发 TG 一天就是
48 条垃圾消息。只有需要人知道的结果才打扰人。

## 依赖的 Secrets

| 名字 | 账号 | 说明 |
|---|---|---|
| `ICEHOST_SERVER_URL` / `_02` | 01 / 02 | 面板地址 |
| `ICEHOST_COOKIES` / `_02` | 01 / 02 | 见下方「Cookie 三种写法」 |
| `ICEHOST_EMAIL` / `_02` | 01 / 02 | Cookie 失效时的兜底登录 |
| `ICEHOST_PASSWORD` / `_02` | 01 / 02 | 同上 |
| `TG_BOT_TOKEN` | — | Telegram 机器人 token（可选） |
| `TG_CHAT_ID` | — | Telegram 会话 id（可选） |

### Cookie 三种写法都吃

`ICEHOST_COOKIES` 可以是下面任意一种，脚本自己认：

```
# 1) JSON 数组（浏览器插件导出）
[{"name": "icehostpl_session", "value": "..."}, {"name": "XSRF-TOKEN", "value": "..."}]

# 2) JSON 对象（顶层直接是 name -> value，或者包一层 "cookies"）
{"icehostpl_session": "...", "XSRF-TOKEN": "..."}

# 3) Cookie 头原文（F12 → Network → Request Headers → Cookie 直接复制）
icehostpl_session=...; XSRF-TOKEN=...
```

必须同时含 `icehostpl_session` 与 `XSRF-TOKEN`，缺哪个脚本会直接点名。

> 注：面板的 WAF 只验 `icehostpl_session` **存在**、不验**有效**，所以 Cookie
> 过期时页面不会跳登录，而是整个变成 `WAF - Block`。脚本会把这种页面识别成
> `FAILED` 并明确提示「请更新 ICEHOST_COOKIES」。实测 Cookie 失效后靠
> `ICEHOST_EMAIL`/`ICEHOST_PASSWORD` 兜底登录是可以正常续期的。

## 手动跑

```bash
# 干跑：只检查不点击，不会消耗续期窗口
gh workflow run direct-renew.yml -f dry_run=true

# 真跑
gh workflow run direct-renew.yml
```

本地验证（不需要浏览器、不需要凭据）：

```bash
RENEWKIT_PATH=/path/to/renew-kit python3 .verify/verify_icehost.py
```

## 目录

```
main.py                              续期引擎（Cookie 优先，失效转密码登录）
scripts/setup_proxy.sh               代理引导 + 出口验证，结果写进 $GITHUB_ENV
.verify/verify_icehost.py            离线验收：纯逻辑单测 + 场景矩阵 + 一致性检查
.github/workflows/direct-renew.yml   主排程（每小时，2 账号矩阵）
.github/workflows/probe.yml          手动：WAF/CF 探路（零凭据，直连/WARP 两条路都测）
.github/workflows/warp-probe.yml     手动：WARP(wgcf+wireproxy) 出口探测
.github/workflows/discover.yml       手动：挖面板 DOM / SPA 路由 / API 端点
probe.py / discover_server.py / scripts/api_probe.py   上面三个工作流的脚本
```
