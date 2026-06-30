# server-watcher

基于 QQ 官方 Bot API / Gateway 的服务器监视机器人。

当前版本先支持一个可扩展命令插件：

- `gpustat`: 查看当前机器 GPU 状态
- `top`: 查看当前 `top` 快照
- `df`: 查看磁盘挂载使用情况
- `du`: 查看目录占用摘要
- `codex`: 桥接到服务器上的交互式 Codex CLI

## 目录

- `src/server_watcher/`: 核心代码
- `scripts/run_qq_server_watcher.py`: 启动入口
- `configs/server_watcher.yaml.example`: 配置模板
- `tests/`: 基础测试

## 环境

当前目录已经创建了虚拟环境 `.venv`。

安装依赖：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

## 配置

复制模板并填写：

```bash
cp configs/server_watcher.yaml.example configs/server_watcher.yaml
```

需要配置：

- `providers.qq.target_type`: `c2c` 或 `group`
- `providers.qq.target`: 允许控制机器人的目标 openid
- `QQBOT_APP_ID`
- `QQBOT_CLIENT_SECRET`

推荐通过环境变量提供密钥：

```bash
export QQBOT_APP_ID=你的_app_id
export QQBOT_CLIENT_SECRET=你的_client_secret
```

## 启动

```bash
.venv/bin/python scripts/run_qq_server_watcher.py --config configs/server_watcher.yaml
```

## 支持命令

- `help`
- `gpustat`
- `gpustat text`
- `gpustat full`
- `gpustat full text`
- `gpustat json`
- `gpustat brief`
- `top`
- `top 30`
- `top text`
- `top full`
- `df`
- `df -h`
- `df inode`
- `df text`
- `du`
- `du -h /raid/lhk`
- `du -h /raid/lhk --max-depth=1`
- `du -h /raid/lhk sort`
- `du -h /raid/lhk sort asc`
- `du -h /raid/lhk image`
- `codex`
- `codex 帮我检查 /raid/lhk/TradePilot 的结构`
- `qqcodex status`
- `qqcodex stop`

默认会把 `gpustat` 的 ANSI 终端输出渲染成图片回复，因此在 QQ 内能看到接近终端的配色效果。
`top` 由于本身是交互程序，这里兼容的是单次快照，不是可滚动会话。
`du` 默认回文本摘要，并按体积排序，主要兼容 `du -h 路径` 这种用法；图片模式仅作为可选项保留。

## Codex Bridge

发送 `codex` 后，会在服务器上启动一个真实的 `codex` 交互会话，并把后续 QQ 消息直接转发到这个会话。

- 支持 Codex 自己的 `/` 指令
- 支持直接发送自然语言需求
- 用 `qqcodex stop` 退出桥接
- 用 `qqcodex status` 查看当前会话
