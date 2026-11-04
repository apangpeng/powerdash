# 用电总览看板 · 网上国网数据对接说明

一个深色电力监控看板（`index.html`）+ 本地自动获取脚本（`fetch_sgcc.py`），把「网上国网」账户的用电量、电费、余额数据取回来显示。

## 目录结构

```
electricity-dashboard/
├── index.html            # 看板页面（唯一入口，浏览器直接打开）
├── data.js               # 看板数据文件（页面所有数字的来源）
├── fetch_sgcc.py         # 获取脚本：网上国网 → data.js
├── serve_dashboard.py    # 本地服务：网页打开看板 + 右上角「登录」一键获取
├── config.json           # 登录配置（手机号/密码，本地保存，勿外传）
├── requirements.txt      # Python 依赖
├── gen_data.py           # 初始示例数据生成脚本（仅用一次）
├── _backup/index-v1.html # 页面旧版本备份
├── debug/                # --dump 模式保存的原始接口响应（便于排查）
└── tools/
    └── sgcc_protocol/    # 协议实现（复制自 MIT 开源项目
                          #   github.com/stevenjoezhang/hass-state-grid）
```

## 工作原理

```
网上国网 App 接口（非公开，需账号密码登录）
        │  fetch_sgcc.py 每天/手动运行一次
        ▼
data.js  ← 页面读取 window.DASHBOARD_DATA 渲染所有数字与图表
```

要点：

- 登录态（token）保存在 `token.json`，有效期约 15 天，过期自动用密码重新登录；
- 首次登录或服务端要求新设备验证时，会向注册手机号发送短信验证码，脚本提示你输入；
- 国家电网日用电数据一般延迟 1~2 天发布（今天取到的是昨天），属正常现象；
- 数据以网上国网账单为准，本工具只适合家庭看板与趋势观察，不作为计费依据。

## 快速开始

### 方式一：网页一键登录（推荐，不用改文件）

1. 安装 Python 依赖：

   ```
   pip install -r requirements.txt
   ```

2. 启动本地服务：

   ```
   python serve_dashboard.py
   ```

3. 浏览器打开 `http://127.0.0.1:8776/`

4. 点页面右上角「登录」，填手机号/密码 → 需要时输入短信验证码 → 页面自动刷新为真实数据。

之后每次要更新数据：先启动 `serve_dashboard.py`，再打开页面点「登录」即可（token 有效期内不会再要求验证码）。

### 方式二：命令行

1. 编辑 `config.json`，填入网上国网登录手机号和密码：

   ```json
   {
     "phone": "138xxxxxxxx",
     "password": "你的密码",
     "account_index": 0,
     "days": 30
   }
   ```

   - `account_index`：同一账号绑了多个户号时，选第几个（0 开始）；
   - `days`：日用电图查询天数（1~90）；
   - `province / city / region`：一般留空即可；若登录报地区相关错误再填（格式参考 95598 地区编码）。

2. 首次运行（会触发短信验证）：

   ```
   python fetch_sgcc.py
   ```

3. 刷新 `index.html` 查看。之后每次运行 `python fetch_sgcc.py` 更新数据即可。

4. 常用参数：

   ```
   python fetch_sgcc.py --days 40     # 改查 40 天
   python fetch_sgcc.py --dump        # 只抓原始响应存到 debug/，不更新页面
   python fetch_sgcc.py --sms 123456  # 已知验证码时直接传入
   python fetch_sgcc.py --send-sms    # 只发验证码并退出（供网页登录流程调用）
   ```

## 定时自动更新（Windows 计划任务）

> **fnOS 应用版（.fpk）已内置每日定时获取**：默认每天 08:00 用现有登录态静默获取一次
> （token 有效期内不重新登录、不影响手机 App 会话）；可用应用数据目录
> `config.json` 的 `daily_refresh`（false 关闭）与 `refresh_hour`（0~23）调整。
> 以下计划任务方式仅适用于 Windows 直跑源码。

## 邮件推送（fnOS 应用版 v1.2.5+，v1.2.6 起页面设置，v1.3.0 频率可选）

页面右上角「⚙ 设置」弹窗可配置：SMTP 服务商下拉点选（QQ/163/自定义，免手输）、发件邮箱、
授权码、收件邮箱、推送频率（每天 / **每周（默认，周一发周报）** / 仅异常）、余额提醒阈值。
「发送测试邮件」即时验证，保存立即生效、不用重启；授权码留空表示保持不变（已设置过时）。

- **每周**：周一 08:00 获取成功后发周报——近 7 天用电合计/日均/环比、本月用电、当前余额、
  上月账单、阶梯档位；
- **仅异常**：平时不发，仅在登录失效、或余额低于设置阈值时提醒（阈值填 0 关闭）；
- 登录失效提醒**任何频率下都会发**，不会漏。

**v1.3.0 起授权码混淆存储**：config.json 中不再保存明文授权码（存为混淆值），
旧版本保存的明文授权码自动兼容（读取时自动识别）；页面/接口只返回「是否已设置」，不回传明文。

如需手改（老版本或偏好配置文件），在应用数据目录 `config.json` 的 `push` 块填写（QQ 邮箱示例）：

```json
"push": {
  "channel": "mail",
  "smtp_host": "smtp.qq.com",
  "smtp_port": 465,
  "smtp_user": "你的QQ号@qq.com",
  "smtp_pass": "QQ邮箱SMTP授权码",
  "mail_to": "收件邮箱（可同发件）",
  "freq": "weekly",
  "balance_threshold": 0
}
```

QQ 邮箱需先在「设置 → 账户 → 开启 SMTP 服务」生成授权码（不是登录密码）；163 则把
`smtp_host`/`smtp_port` 换成 `smtp.163.com`/`465`。不填 `smtp_user`/`smtp_pass`/`mail_to`
则不发送。

## 访问口令（v1.1.0+，v1.2.6 起会话持久化）

首次打开页面时设置 8 位以上访问口令；口令正确后浏览器 7 天内免输入。
**v1.2.6 起会话改为存应用数据目录（`pd_sessions.json`，只存 token 哈希）**，应用升级/重启后
不再要求重新输入口令；换浏览器/换设备仍需重新验证。防爆破：同一 IP 错 5 次锁 15 分钟、
锁期翻倍。

1. 打开「任务计划程序」→ 创建任务；
2. 触发器：每天 08:00（国网日数据一般凌晨发布，早上抓最稳）；
3. 操作 → 新建 → 程序填 `python`，参数填脚本绝对路径：

   ```
   "C:\...\electricity-dashboard\fetch_sgcc.py"
   ```

4. 起始于填 `C:\...\electricity-dashboard`；
5. 「条件」页取消勾选「只有在计算机使用交流电源时才启动」，避免笔记本睡眠漏跑。

> 注意：国网每天登录次数有限，脚本会复用 token 避免频繁登录；若连续多天失败，检查是否因验证码/风控触发，隔几小时再试。

## 在飞牛 fnOS 上部署（免 Docker）

1. 将分发 zip 解压到 fnOS 任意目录（如 `/vol1/apps/electricity-dashboard`）；
2. 安装 Python 依赖：`pip3 install -r requirements.txt`；
3. 启动：`python3 serve_dashboard.py`；
4. 局域网访问 `http://<飞牛IP>:8776/`，右上角「登录」绑定你自己的网上国网账号；
5. 开机自启：在 fnOS「命令任务/自启动」里添加上述启动命令，或用 systemd 配置。

可调环境变量（可选）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHBOARD_HOST` | `127.0.0.1` | 监听地址；局域网访问设为 `0.0.0.0`（仅限可信内网） |
| `DASHBOARD_PORT` | `8776` | 端口 |
| `DASHBOARD_DATA_DIR` | 应用目录 | 账号配置/数据目录，可放到独立磁盘目录便于备份 |

> 分发包已清空所有账号信息（config.json 为空、无 token、示例数据），首次登录需输入你自己的网上国网账号。

## 手动兜底（脚本挂了怎么办）

看板本身就是纯静态页面：直接编辑 `data.js` 里的数值（对照网上国网 App 读数填入），保存后刷新页面即可。`data.js` 里每一项都有对应说明，结构见下方映射表。

## 数据字段映射

| 页面位置 | data.js 字段 | 数据来源 |
|---|---|---|
| 本月用电 | `kpis[0]` | 当月日用电量合计 |
| 最新日用电 | `kpis[1]` | 上游最新一条日用电（T-1~2） |
| 账户余额 | `kpis[2]` / `summary` / `details` | 余额接口（预付费余额/后付费应交） |
| 日均用电 / 预计月末 | `summary[0] / [1]` | 由本月累计与最新日期推算 |
| 上月结余 | `summary[2]` | 接口无此数据，显示 — |
| 上月用电 / 电费 | `stats[0]/[1]` | 最近出账月账单 |
| 本年电费 | `stats[2]` | 本年累计账单 |
| 每日用电图 | `charts.daily` | 日用电接口 |
| 分时图（尖峰平谷） | `charts.tou` | 日用电接口的 `tip/peak/flat/valley` 字段 |
| 月度电量电费图 | `charts.monthly` | 本年月度账单 |
| 打码示例标记 | `meta.mock`、`masked` | 真实数据为 false，自动隐藏「示例数据」角标 |

## 排障

| 现象 | 处理 |
|---|---|
| `config.json 中 phone / password 为空` | 先在 config.json 填好登录信息 |
| 提示输入验证码但没收到短信 | 确认手机号正确；国网每天短信次数有限，隔一段时间再试 |
| `RK008：安全校验未通过` | 官方 App 登录风控触发。先在「网上国网」App 完成登录验证，隔数小时再运行 |
| `登录失败（-200/-201）` | 账号密码错误或凭证失效，检查 config.json |
| `账户列表为空` | 确认网上国网账号已绑定用电户号 |
| 数据日期落后 1~2 天 | 正常，上游日数据延迟发布 |
| 接口报错/字段缺失 | 运行 `python fetch_sgcc.py --dump` 保存原始响应，对照字段排查 |
| 服务端协议升级导致脚本失效 | 到 `github.com/stevenjoezhang/hass-state-grid` 拉取新版，重新复制协议模块到 `tools/sgcc_protocol/`（见下） |
| 页面点「登录」提示未检测到本地服务 | 先运行 `python serve_dashboard.py`，再用浏览器打开 `http://127.0.0.1:8776/`；不要直接双击 `index.html` |
| 8776 端口被占用 | 修改 `serve_dashboard.py` 顶部的 `PORT` 为其他端口，同时把页面登录脚本中写死的 `127.0.0.1:8776` 改掉 |
| 报错 `cannot reach the State Grid App gateway` | 国网网关使用自签名证书，脚本已内置禁用证书校验（只针对该固定地址，流量仍为 TLS 加密）；若仍出现请检查网络/DNS 能否解析 `csc-service.sgcc.com.cn` |
| 登录失败（0100 账号或密码错误） | 手机号或密码不对。请使用「网上国网」App 的登录密码（注册时设置的那个，不是服务密码/初始密码）；**连续错误 5 次账号会锁定 30 分钟**，确认无误再试 |

## 更新协议实现

协议模块来自开源项目 `stevenjoezhang/hass-state-grid`（MIT 协议，官方是 Home Assistant 集成，本目录只复制了其中与请求协议相关的文件）。升级步骤：

```
下载新版仓库 zip → 解压 → 复制 custom_components/state_grid/ 下的
api.py crypto.py login.py models.py synthetic_device.py const.py turing/
到 tools/sgcc_protocol/ 覆盖同名文件
```

## 隐私与合规提醒

- `config.json`（手机号、密码）与 `token.json`（登录令牌）保存在本目录，**不要上传到公开位置**、不要放入任何云盘同步目录之外的可公开仓库；
- 网页登录方式下，密码同样只保存在本机 `config.json`；本地服务只绑定 `127.0.0.1`（仅本机可访问），**请勿通过任何方式把它暴露到局域网或公网**；
- 本工具仅读取你自己的账户数据，不用于任何计费、结算或其他需要法律效力的场景；
- 接口为非公开接口，服务端升级后可能失效；本项目不对其稳定性做任何保证。

## 适用范围

适用于国家电网（网上国网）覆盖省份。**南方电网省份（广东、广西、云南、贵州、海南）不适用。**
