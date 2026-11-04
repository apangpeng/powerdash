# 用电总览 · 电力监控看板（powerdash）

面向 fnOS 的原生应用（fpk 包），对接「网上国网」账户，自动获取并展示用电量、电费、余额、尖峰平谷等数据；支持每日自动更新、可选频率邮件推送与 AI 用电解读（仅上传聚合用电数据）。

当前版本：v1.4.1

## 目录

| 目录 | 说明 |
|---|---|
| `electricity-dashboard/` | 源码树：本地服务 `serve_dashboard.py` + 数据获取 `fetch_sgcc.py` + 前端页面 |
| `electricity-dashboard-fpk/` | fnOS fpk 打包模板（manifest / cmd / ui / app / scripts） |

## 快速开始（源码直跑）

```
pip install -r electricity-dashboard/requirements.txt
python electricity-dashboard/serve_dashboard.py
```

浏览器打开 http://127.0.0.1:8776/，右上角「登录」绑定你自己的网上国网账号。

## 构建 fpk

Windows：`powershell -File electricity-dashboard-fpk/scripts/build.ps1`（需在 `electricity-dashboard-fpk/` 放置 `fnpack.exe`，从 fnOS 官方渠道获取）；产物为 `electricity-dashboard-fpk/dist-fpk/powerdash-<version>-x86.fpk`。

## 安全与隐私

- 仓库不含任何真实凭据：`config.json` 为空白模板；`device_state.json`（设备令牌）、`token.json`（登录态）、`pd_sessions.json`（口令会话）均为运行时生成、存于应用数据目录，不入库。
- 网页/接口只回脱敏信息（手机号尾号、是否已设置授权码/API Key）。
- AI 解读只把聚合用电数据发给云端 LLM；国网账号密码、访问口令、SMTP 授权码永不出 NAS。

## 协议来源

网上国网通信协议实现取自 MIT 开源项目 [stevenjoezhang/hass-state-grid](https://github.com/stevenjoezhang/hass-state-grid)，本仓库 `electricity-dashboard/tools/sgcc_protocol/` 为其精简拷贝；升级方式见 `electricity-dashboard/README.md`。

## 变更记录

- v1.4.1 修复 AI 解读把无读数日期误当 0 用电（抓取端保留 null + prompt 标注「无数据」）
- v1.4.0 新增 AI 用电解读（DeepSeek/通义/豆包/自定义 OpenAI 兼容），修复版本角标
- v1.3.1 修复 fnOS 内嵌界面 data.js 加载（CGI 反代 + 相对路径）
- v1.3.0 邮件推送频率可选（每天/每周/仅异常）+ SMTP 服务商下拉 + 授权码混淆存储
- 更早版本见各目录 README
