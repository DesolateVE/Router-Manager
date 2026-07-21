# Router Manager

面向 OpenWrt 的双核心代理管理服务。Router Manager 使用 FastAPI 提供管理 API 与 Web 界面，负责维护节点、规则和端口绑定，并管理 Mihomo 与 sing-box 两个核心进程。

Mihomo 用于完整的透明代理、策略组与规则路由；sing-box 用于将指定本地端口固定转发到某个代理节点。两者共用节点数据，但分别生成自己的配置文件。

## 功能

- 节点管理：支持 Shadowsocks、VMess、VLESS、Trojan、Hysteria2、TUIC 等常用节点。
- 多格式导入：支持 URI、订阅链接、Clash YAML、纯 YAML 代理块与 sing-box outbound JSON。
- Mihomo 配置：策略组、主规则、嵌套子规则、规则提供者、DNS、设备绑定和流量隧道。
- sing-box 端口绑定：为一个本地 mixed / SOCKS / HTTP 入站端口指定独立出口节点。
- 双核心管理：查看状态、启动、停止、重启 Mihomo 与 sing-box。
- 双日志面板：在页面中切换查看 Mihomo 或 sing-box 的最近 500 行标准输出日志。
- 配置应用：Mihomo 使用 API 热重载；变更 sing-box 配置时自动重启 sing-box。
- 仪表盘：提供 Mihomo MetaCubeXD 与 sing-box MetaCubeXD 两个独立入口。

## 架构与面板说明

```text
浏览器
  └─ Router Manager :8080
       ├─ Web 管理界面 / REST API
       ├─ MetaCubeXD（Mihomo） ──────> Mihomo :9090
       └─ MetaCubeXD（sing-box） ────> sing-box :9091（仅本机监听）
```

部署脚本会将随项目提供的 MetaCubeXD 静态资源解压到数据目录。管理服务再将它分别代理给两个核心，因此无需在浏览器中直接访问核心 API 端口，也避免了跨域和防火墙问题。

sing-box 官方 Dashboard 已存在，但它依赖 sing-box 1.14 引入的新 gRPC API 服务；本项目当前使用兼容稳定版本的 Clash API。因此这里为 sing-box 提供的是独立的 MetaCubeXD 实例，而不是将不存在的官方页面误指向 `/ui`。

## 目录结构

```text
Router-Manager/
├── main.py              # FastAPI 入口与进程退出处理
├── models.py            # 数据模型与默认设置
├── store.py             # data.json 的线程安全读写
├── config_gen.py        # Mihomo YAML 配置生成
├── singbox_gen.py       # sing-box JSON 配置生成
├── import_engine.py     # 节点与订阅解析
├── process_mgr.py       # 两个核心的进程与日志管理
├── api/routes.py        # REST API、面板入口与反向代理
├── web/                 # 原生 JavaScript 管理界面
├── resource/            # Mihomo、MetaCubeXD 与部署资源
├── deploy.py            # SSH 部署脚本
└── router_manager.init  # OpenWrt procd 服务模板
```

## 本地运行

需要 Python 3.9+ 与 pip。

```bash
python -m pip install -r requirements.txt
python main.py --data-dir ./data --port 8080
```

打开 `http://127.0.0.1:8080`。本地运行不会自动安装或提供 Mihomo / sing-box；请在设置中填写可执行文件路径，或通过命令行显式启用自动启动：

```bash
python main.py --data-dir ./data --port 8080 --auto-start-mihomo --auto-start-sing-box
```

### 启动参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--host` | `0.0.0.0` | 管理服务监听地址 |
| `--port` | `8080` | 管理服务端口 |
| `--data-dir` | `/etc/router_manager` | 数据、生成配置与面板资源目录 |
| `--auto-start-mihomo` | 关闭 | 服务启动时拉起 Mihomo |
| `--auto-start-sing-box` | 关闭 | 服务启动时拉起 sing-box |

## OpenWrt 部署

部署目标需使用 `apk` 包管理器、可联网访问软件源，并预留足够的存储空间。部署器会检查并安装缺失的 `python3`、`py3-pip` 和 `sing-box`；项目自带 Mihomo 二进制及 MetaCubeXD 资源。

本机需要 Python 3。使用 SSH 密钥时还需 `ssh` / `scp`；使用密码时需安装 Paramiko：

```bash
python -m pip install paramiko
```

### 一键部署

```bash
python deploy.py
python deploy.py --host 10.0.8.84 --user root
python deploy.py --ip 10.0.8.84 --username root --password "你的密码"
```

默认目标地址为 `10.0.8.84`，服务端口为 `8080`。部署器会停止旧服务、上传代码和资源、解压 MetaCubeXD、写入 `/etc/init.d/router_manager`、设置开机自启并启动两个核心。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host`, `--ip` | `10.0.8.84` | 设备地址 |
| `--user`, `--username` | `root` | SSH 用户 |
| `--password`, `-p` | 空 | SSH 密码；留空时使用密钥或 agent |
| `--target-dir` | `/opt/router_manager` | 程序目录 |
| `--data-dir` | `/etc/router_manager` | 数据与核心配置目录 |
| `--port` | `8080` | Web 管理端口 |
| `--connect-timeout` | `10` | SSH 连接超时（秒） |

部署完成后访问 `http://设备地址:8080`。

### 服务管理与日志

```sh
/etc/init.d/router_manager status
/etc/init.d/router_manager start
/etc/init.d/router_manager stop
/etc/init.d/router_manager restart

# 系统日志中区分两个核心
logread | grep '\[mihomo\]'
logread | grep '\[sing-box\]'
```

日常排障优先使用 Web 界面的“日志”页：选择对应核心后可刷新、自动滚动或清空该核心的内存日志缓冲。

## 使用流程

1. 在“代理”页导入或添加节点。
2. 如使用 Mihomo，在“策略组”“规则”“DNS”等页面完成主配置。
3. 如使用 sing-box，在“端口绑定”页添加监听端口并选择节点。
4. 点击顶部“应用配置”：Mihomo 会热重载，sing-box 会重启以载入新 JSON。
5. 在总览页按需打开对应的 MetaCubeXD，或在“日志”页确认两个核心状态。

保存设置、节点或端口绑定后，顶部会提示有待应用的配置；未点击“应用配置”前，运行中的核心不会读取这些改动。

## 数据与配置文件

默认数据目录为 `/etc/router_manager`：

| 文件 / 目录 | 用途 |
|---|---|
| `data.json` | Router Manager 的持久化状态 |
| `config.yaml` | 生成的 Mihomo 配置 |
| `sing-box.json` | 生成的 sing-box 配置 |
| `template.yaml` | Mihomo 基础模板；动态节点、组与规则会在此基础上叠加 |
| `metacubexd-gh-pages/` | 部署时解压的 MetaCubeXD 静态文件 |

不要直接手改 `config.yaml` 或 `sing-box.json`，因为下一次应用配置时会覆盖它们。需要调整 Mihomo 基础配置时，请修改 `template.yaml`；节点、规则和端口绑定请使用管理界面或 API 修改。

## 主要 API

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/status` | GET | 两个核心的运行状态、PID 与 API 端口 |
| `/api/start`、`/api/stop`、`/api/restart` | POST | 控制 Mihomo |
| `/api/sing-box/start`、`/api/sing-box/stop`、`/api/sing-box/restart` | POST | 控制 sing-box |
| `/api/logs` | GET / DELETE | 读取或清空 Mihomo 日志 |
| `/api/sing-box/logs` | GET / DELETE | 读取或清空 sing-box 日志 |
| `/api/apply` | POST | 应用 Mihomo 和 / 或 sing-box 配置 |
| `/api/config/preview` | GET | 预览 Mihomo YAML |
| `/api/sing-box/config/preview` | GET | 预览 sing-box JSON |
| `/api/settings` | GET / PUT | 读取或保存全局设置 |
| `/api/proxies`、`/api/groups`、`/api/rules` | CRUD | 节点、策略组、主规则 |
| `/api/sub-rules`、`/api/rule-providers` | CRUD | 子规则集、规则提供者 |
| `/api/port-bindings` | CRUD | sing-box 固定端口绑定 |
| `/api/import/*` | POST | URI、文本、订阅、Clash YAML 或 sing-box JSON 导入 |

## 依赖

- FastAPI
- Uvicorn
- Pydantic 2
- PyYAML
- HTTPX

版本范围见 [requirements.txt](requirements.txt)。
