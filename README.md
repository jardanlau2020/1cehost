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
打开面板 → 注入 Cookie → 物理点击过 CF 盾 → 识别 WAF/CF 拦截
        → 判定登录态（失效则用账号密码兜底登录）
        → 读续期前到期时间 → 点续期按钮 → 读续期后到期时间
        → 差值是 +5~9 小时才算真续上
```

> **WAF 判定必须在注入 Cookie 之后**，这是踩过坑的：面板 WAF 只认
> `icehostpl_session`「存在」与否（不验有效性），首次裸访必然吃 `WAF - Block`。
> 把判定放在注入之前，等于每次运行都误报。实盘 run #241 就是这么红的 ——
> 代理和 Cookie 都好好的，脚本在注入前就把自己判死了，一次都没点到。
> 所以 `classify_page()` 的 `has_proxy` 是**必填 kwarg**：忘了传直接 `TypeError`，
> 不会静默走错分支。

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
| `TRANSIENT` | 上游故障（CF 挑战页、5xx、超时）+ **直连被 WAF 拦** | 0 | ❌ 静默 |
| `UNKNOWN` | 点了但读不到明确结果 | 0 | ✅ |
| `FAILED` | 真失败，需人工（走代理仍被 WAF 拦 / Cookie+密码都失效 / 按钮消失 / 被停权） | 1 | ✅ |

**跳过时静默是有意的**：本仓每小时跑一次，窗口没开是常态，每次都发 TG 一天就是
48 条垃圾消息。只有需要人知道的结果才打扰人。

**WAF 拦截按出口分两档**（`classify_page(page_source, has_proxy=...)`）：

| 出口 | 结论 | 为什么 |
|---|---|---|
| 走了代理仍被拦 | `FAILED`（exit 1，发 TG） | 出口 IP 是干净的，就只剩 cookie 废了这一种解释 → 要人换 |
| 直连被拦 | `TRANSIENT`（exit 0，静默） | runner 机房 IP 本来就在 WAF 黑名单里，挂上代理就好，下个小时再试 |

直连那档额外打一条 `::warning::` 注解（Actions 摘要里的黄色感叹号），
把「代理没起来」和「WAF 拦」对上号 —— 否则它只是日志里一行不起眼的
`no proxy, direct mode`，人要翻很久。

## 代理（sing-box）与 `NODE_LINK`

`scripts/setup_proxy.sh` 干三件事：装 sing-box → **真连一次 `api.ipify.org`
验证出口**（不是只看进程在不在）→ 把结果写进 `$GITHUB_ENV` 给后面的 step 用。

它自己不解析节点，节点是上游 installer 解析的，入口是：

```bash
export NODE_LINK=${NODE_LINK:-''}
if [ -z "$NODE_LINK" ]; then
  echo "[INFO] 未配置代理，直连模式"     # ← 一声不响地退直连
```

> ⚠️ 所以 workflow 的 `env:` 里**必须**有 `NODE_LINK: ${{ secrets.NODE_LINK }}`。
> 迁移到 renew-kit 时漏过这一行，上游 installer 取到空值直接退直连，出口换成
> runner 机房 IP，被面板 WAF 拦掉 —— 实盘 run #241 的红就是这个。
> `setup_proxy.sh` 现在会在第 0 步检查并显式告警，别再让它烂在日志里。

代理整体是**尽力而为**：装不成、探不通都退回直连，`setup-command` 保持默认的
`continue-on-error: true`，绝不因为代理挂了而中断续期。

## 依赖的 Secrets

| 名字 | 账号 | 说明 |
|---|---|---|
| `ICEHOST_SERVER_URL` / `_02` | 01 / 02 | 面板地址 |
| `ICEHOST_COOKIES` / `_02` | 01 / 02 | 见下方「Cookie 三种写法」 |
| `ICEHOST_EMAIL` / `_02` | 01 / 02 | Cookie 失效时的兜底登录 |
| `ICEHOST_PASSWORD` / `_02` | 01 / 02 | 同上 |
| `NODE_LINK` | — | sing-box 节点 URI，交给 `scripts/setup_proxy.sh`；**不配就会退直连** |
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

> 注：面板的 WAF 只验 `icehostpl_session` **存在**、不验**有效**。这带来两个后果：
> 一是**首次裸访必然被拦**（所以判定必须在注入 Cookie 之后，见上）；二是 Cookie
> 真的过期时，页面不会跳登录，而是整个变成 `WAF - Block` —— 这时如果代理是通的，
> 脚本会判 `FAILED` 并明确提示「请更新 ICEHOST_COOKIES」。实测 Cookie 失效后靠
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
