# Router Manager

Mihomo（Clash Meta）核心的 Web 管理面板，基于 Python/FastAPI 实现。提供 REST API 与原生 JS 前端，用于代理节点管理、策略组配置、路由规则编排以及进程控制。

---

## 功能特性

- **代理节点管理** — 支持 Shadowsocks、VMess、VLESS、Trojan、Hysteria2
- **多格式节点导入** — URI、订阅链接、Clash YAML、base64 批量导入
- **策略组配置** — select / url-test / fallback / load-balance / relay
- **路由规则编排** — 支持 `SUB-RULE` 嵌套子规则树，可拖拽排序
- **DNS 配置** — fake-ip / redir-host 模式，支持自定义上游/备用 DNS
- **进程管理** — 启动、停止、重启 Mihomo 核心，实时查看运行状态
- **热重载** — 写出配置文件后通过 Mihomo API 在线重载，无需重启
- **配置预览** — 实时预览生成的 YAML 配置内容

---

## 目录结构

```
router_manager/
├── main.py           # 入口，FastAPI 应用 & CLI 参数
├── models.py         # Pydantic 数据模型
├── store.py          # 线程安全持久化（data.json）
├── config_gen.py     # Mihomo YAML 配置生成器
├── import_engine.py  # 节点导入引擎
├── process_mgr.py    # Mihomo 进程管理
├── api/
│   └── routes.py     # REST API 路由
├── web/
│   ├── index.html    # 前端页面
│   ├── app.js        # 前端交互逻辑
│   └── style.css     # 暗色主题样式
├── requirements.txt
├── deploy.py         # OpenWrt SSH 部署脚本
└── README.md
```

---

## 本地运行

### 环境要求

- Python 3.9+
- pip

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
python main.py
```

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `9080` | 监听端口 |
| `--data-dir` | `/etc/router_manager` | 数据目录（存放 data.json、config.yaml） |

示例：

```bash
python main.py --data-dir ./data --port 9080
```

启动后浏览器访问 `http://localhost:9080`。

---

## 部署到 OpenWrt

### 前提条件

本机已安装 `python3`。不填写密码时，还需要本机可用的 `ssh` / `scp` 和 SSH 密钥。

如需使用密码登录，还需要安装 Python 包 `paramiko`：

```bash
pip install paramiko
```

目标设备需满足：
- 已联网，可访问 opkg 源
- 存储空间充足（Python3 + 依赖约 80MB）

### 一键部署

```bash
python deploy.py
```

指定 IP、账号和密码：

```bash
python deploy.py --ip 10.0.8.84 --user root --password weiyi
```

不传 `--password` 时，脚本会直接使用本机 SSH 密钥或 ssh-agent 连接：

```bash
python deploy.py --ip 10.0.8.84 --user root
```

脚本会自动完成以下操作：

1. 上传项目文件到 `/opt/router_manager/`
2. 上传资源文件到 `/etc/router_manager/`
3. 设置防火墙脚本和 `mihomo` 核心执行权限
4. 写入 procd init 脚本 `/etc/init.d/router_manager`
5. 设置开机自启并立即启动服务，服务启动时自动拉起 `mihomo`

部署成功后访问：`http://10.0.8.84:8080`

### 手动管理服务

```bash
# SSH 登录到设备
ssh root@10.0.8.84   # 密码: weiyi

# 查看状态
/etc/init.d/router_manager status

# 启动 / 停止 / 重启
/etc/init.d/router_manager start
/etc/init.d/router_manager stop
/etc/init.d/router_manager restart

# 查看日志
logread | grep mihomo
```

### 部署参数说明

| 参数 | 默认值 | 说明 |
|------|----|------|
| `--host`, `--ip` | `10.0.8.84` | 目标设备 IP / 主机名 |
| `--user`, `--username` | `root` | SSH 用户名 |
| `--password`, `-p` | 空 | SSH 密码；不填写则使用 SSH 密钥 |
| `--target-dir` | `/opt/router_manager` | 程序安装目录 |
| `--data-dir` | `/etc/router_manager` | 数据目录 |
| `--port` | `8080` | Web UI 端口 |
| `--connect-timeout` | `10` | SSH 连接超时时间（秒） |

使用 `python deploy.py --help` 可以查看完整参数说明。

---

## 配置文件模板

将自定义的 Mihomo 基础配置放在 `DATA_DIR/template.yaml`，配置生成器会以此为底板叠加动态字段（节点、策略组、规则等）。

---

## API 参考

| 路径 | 方法 | 功能 |
|------|------|------|
| `/api/status` | GET | 运行状态与 PID |
| `/api/start` | POST | 启动 Mihomo |
| `/api/stop` | POST | 停止 Mihomo |
| `/api/restart` | POST | 重启 Mihomo |
| `/api/reload` | POST | 热重载配置 |
| `/api/config/preview` | GET | 预览生成的 YAML |
| `/api/settings` | GET/PUT | 读写全局设置 |
| `/api/proxies` | GET/POST/PUT/DELETE | 代理节点 CRUD |
| `/api/groups` | GET/POST/PUT/DELETE | 策略组 CRUD |
| `/api/rules` | GET/POST/PUT/DELETE | 路由规则 CRUD |
| `/api/rules/reorder` | PUT | 规则排序 |
| `/api/import/uri` | POST | 导入单条 URI |
| `/api/import/text` | POST | 批量文本导入 |
| `/api/import/subscription` | POST | 订阅链接导入 |
| `/api/import/clash` | POST | Clash YAML 导入 |
| `/api/import/yaml` | POST | 纯 YAML 代理块导入 |

---

## 依赖

| 包 | 版本 |
|----|------|
| fastapi | >=0.104.0 |
| uvicorn[standard] | >=0.24.0 |
| pyyaml | >=6.0 |
| httpx | >=0.25.0 |
| pydantic | >=2.0.0 |
