# MIMO-Router

本地反向代理服务器，将多个 Mimo API key 聚合成一个统一入口，自动选择可用 key 并转发请求。

## 功能特性

- **多 Key 聚合** — 将多个 API key 合并为单一入口，统一管理
- **智能负载均衡** — Round-robin 轮询可用 key，均匀分摊请求
- **自动故障转移** — key 失效时自动切换到下一个可用 key
- **热更新配置** — 修改 `config.json` 后自动生效，无需重启
- **后台健康检查** — 每 30 秒探测 key 状态，自动恢复已修复的 key
- **流式代理** — 支持 SSE 流式响应，逐 chunk 转发
- **双端点支持** — 同时支持 CN 和 SGP 区域端点

## 快速开始

### 环境要求

- Python 3.8+
- aiohttp >= 3.9.0

### 安装

```bash
git clone https://github.com/qiansekai/MIMO-Router.git
cd MIMO-Router
pip install aiohttp
```

### 配置

复制示例配置文件并填入你的 API key：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "apikeys": {
    "cn": [
      {"key": "tp-your-key-here", "status": "valid"}
    ],
    "sgp": [
      {"key": "tp-your-key-here", "status": "valid"}
    ]
  },
  "local_key": "your-local-password",
  "endpoints": {
    "cn": "https://api-cn.xiaomimimo.com",
    "sgp": "https://api-sgp.xiaomimimo.com"
  },
  "port": 18888
}
```

### 启动

```bash
# 方式 1：直接运行
python server.py

# 方式 2：使用启动脚本（Windows）
start.bat
```

服务启动后监听 `http://localhost:18888`

## 使用方法

### 作为 API 代理

将请求发送到本地代理，使用 `local_key` 作为认证：

```bash
curl http://localhost:18888/v1/chat/completions \
  -H "Authorization: Bearer your-local-password" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### Claude Code 集成

在 Claude Code 中配置使用 MIMO-Router：

```json
{
  "api_base_url": "http://localhost:18888",
  "api_key": "your-local-password"
}
```

## Key 管理

使用 `mimo-keys.py` 管理 API key：

```bash
# 检测所有 key 有效性
python mimo-keys.py check

# 仅显示结果，不更新配置
python mimo-keys.py check --dry-run

# 导入单个 key
python mimo-keys.py import tp-xxxxx

# 批量导入（并行检测）
python mimo-keys.py import key1 key2 key3

# 导入 base64 编码的 key
python mimo-keys.py import <base64字符串> --base64
```

### Key 状态说明

| 状态 | 说明 |
|------|------|
| `valid` | 正常使用 |
| `invalid` | 失效（自动标记或手动） |
| `disabled` | 手动禁用 |
| `quota_exhausted` | 额度用尽 |

## 架构

```
┌─────────────────┐     ┌─────────────────┐
│   Claude Code   │────▶│   MIMO-Router   │
│   / 其他客户端   │     │  localhost:18888 │
└─────────────────┘     └────────┬────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
            ┌───────────────┐        ┌───────────────┐
            │   CN 端点     │        │   SGP 端点    │
            │  多个 API Key │        │  多个 API Key │
            └───────────────┘        └───────────────┘
```

### 请求流程

1. 客户端发送请求到 `localhost:18888`
2. 验证 `Authorization` 头中的 `local_key`
3. Round-robin 选择可用 key
4. 转发请求到对应区域端点
5. 流式返回响应
6. 如果失败，自动重试下一个 key

## 配置说明

### config.json 结构

| 字段 | 说明 |
|------|------|
| `apikeys.cn[]` | CN 区域 API key 列表 |
| `apikeys.sgp[]` | SGP 区域 API key 列表 |
| `local_key` | 本地代理认证密码 |
| `endpoints.cn` | CN 区域 API 端点地址 |
| `endpoints.sgp` | SGP 区域 API 端点地址 |
| `port` | 代理监听端口（默认 18888） |

### 环境变量

- `MIMO_CONFIG_PATH` — 自定义配置文件路径（默认 `./config.json`）

## 日志

日志文件 `mimo-router.log` 保存在项目目录，自动清理 2 天前的条目。

## 常见问题

### Q: 如何添加新的 API key？

```bash
python mimo-keys.py import tp-your-new-key
```

### Q: 如何查看哪些 key 可用？

```bash
python mimo-keys.py check
```

### Q: 配置修改后需要重启吗？

不需要，服务器会自动检测 `config.json` 变更并热更新。

### Q: 支持哪些 API 端点？

支持所有兼容 OpenAI API 格式的端点，包括：
- `/v1/chat/completions`
- `/v1/completions`
- `/v1/embeddings`
- `/v1/models`

### Q: 如何在多个客户端间共享 key？

所有连接到 `localhost:18888` 的客户端共享同一个 key 池，自动负载均衡。

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

## 致谢

感谢所有贡献 API key 的社区成员！
