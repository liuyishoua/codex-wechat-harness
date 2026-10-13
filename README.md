# Codex ↔ Hermes 微信桥接

把 **Hermes 已绑定的微信**接到 **现有 Codex App Server 会话**：微信发文字和图片，Codex 在同一任务里处理，再把结果发回微信。支持会话切换、图片双向传递和需要用户回答时的微信通知。

Hermes 管微信登录与收发；本项目只做桥接。不会另起 Hermes 模型任务，不直接轮询微信 getupdates，不替换 Codex 的模型或权限配置。

```text
微信用户 ⇄ Hermes Weixin adapter ⇄ 本地桥接 ⇄ Codex App Server
             登录 / 图片下载       去重 / 会话选择      原会话续接
```

## 支持什么

- 文字、纯图片、带文字图片和多图输入；运行中补充，空闲时续接。
- 自动接上项目最近的可输入会话，或者手动新建、切换。
- Codex 结果文字和本地图片回传；显式发送图片命令。
- 编号问题回到提问的原会话，不受当前聊天会话切换影响。
- SQLite 持久化、微信身份校验、发送去重、断线等待与恢复。

## 运行前提

1. Linux 或 macOS、Python 3.11+。桥接使用 Unix socket；自动生成的后台服务使用 Linux systemd 用户服务，macOS 可先以前台运行。
2. 已安装 **Hermes Agent**，并已启用、绑定自己的 Weixin 通道。需要 Hermes 提供 `pre_gateway_dispatch` hook、`gateway.platforms.weixin.send_weixin_direct` 和 `media_files` 参数；在 Hermes 0.21.5 上验证过。
3. 已登录 Codex，且有一个本机可访问的 **App Server Unix WebSocket**。版本须支持 `thread/list`、`thread/resume`、`thread/turns/list`、`turn/start`、`turn/steer` 和 `localImage`。
4. 桥接、Hermes 与 Codex 使用同一台机器、同一用户可读取的图片路径。

这是连接已存在服务的桥接，不包含 Hermes/Codex 安装、微信扫码登录或平台凭据。Hermes 项目：<https://github.com/NousResearch/hermes-agent>。

## 安装

```bash
git clone https://github.com/liuyishoua/codex-wechat-harness.git
cd codex-wechat-harness

# 使用能导入 Hermes 的 Python；以下是常见的 Hermes 安装目录。
HERMES_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python"
"$HERMES_PYTHON" -m pip install -e .
```

请保留源码目录，采用上面的可编辑安装；Hermes 插件通过符号链接加载源码。

准备这几个值：

| 参数 | 含义 |
| --- | --- |
| `--project-cwd` | 要通过微信操作的项目绝对路径 |
| `--codex-socket` | 现有 Codex App Server 的控制 socket 绝对路径；不是桥接的 `bridge.sock` |
| `--chat-id` | Hermes 入站事件 `source.chat_id` 中的本人微信会话 ID |
| `--user-id` | Hermes 入站事件 `source.user_id` 中的授权用户 ID；可重复参数 |

ID 应使用 Hermes 的实际值，不能填微信昵称。不要上传含登录凭据的完整日志。

```bash
"$HERMES_PYTHON" -m harness.setup \
  --project-cwd /absolute/path/to/project \
  --codex-socket /absolute/path/to/codex-control.sock \
  --chat-id YOUR_BOUND_WECHAT_CHAT_ID \
  --user-id YOUR_WECHAT_USER_ID
```

安装命令生成私有 `.local/config.json`、用户服务文件，并把 `hermes_plugin/` 链接到 `~/.hermes/plugins/codex-wechat-harness`。已有配置或不同来源的同名插件会被保留，不自动覆盖。Hermes 在其他位置时，可传 `--hermes-home`、`--hermes-source` 和 `--python`。

```bash
hermes plugins enable codex-wechat-harness
"$HERMES_PYTHON" -m harness.service
```

在另一个终端检查：

```bash
"$HERMES_PYTHON" -m harness.cli health
```

应看到 `codex_connected: true`。桥接不重启 Hermes 主网关；若当前 Hermes 版本启用插件后未自动热加载，可用其控制接口加载：

```bash
"$HERMES_PYTHON" - <<'PY'
from pathlib import Path
from hermes_cli.config import get_hermes_home
from gateway.control_socket import reload_gateway_plugins
result = reload_gateway_plugins(Path(get_hermes_home()))
print('reloaded:', bool(result and result.get('reloaded')))
PY
```

现在给绑定的机器人发送「help」，然后发一条文字或图片验证真实收发。

### Codex socket

优先使用已有 App Server 的 Unix WebSocket 地址，以续接桌面会话。不同版本和宿主的路径不同，以宿主配置为准。运行 `codex app-server --help` 检查版本：本机验证版本支持 `--listen unix://PATH`。

如果没有运行中的 App Server，可按本机版本支持的方式启动一个：

```bash
mkdir -p "$HOME/.codex"
chmod 700 "$HOME/.codex"
codex app-server --listen "unix://$HOME/.codex/wechat-control.sock"
```

把该路径传给 `--codex-socket`。新启动的 App Server 是否可直接续接某个桌面会话，取决于宿主和 Codex 版本；不要把桥接健康状态当作所有桌面任务都可控制的证明。

### Linux 后台运行

先停止前台桥接，再安装生成的**用户服务**：

```bash
mkdir -p "$HOME/.config/systemd/user"
cp .local/codex-wechat-harness.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now codex-wechat-harness
journalctl --user -u codex-wechat-harness -n 30 --no-pager
```

退出登录后仍要保持运行时，按系统管理方式为该用户启用 linger。示例模板在 `deploy/`；不要同时运行两份桥接。

## 微信命令

| 消息 | 行为 |
| --- | --- |
| `help` / `帮助` | 查看说明 |
| 普通文字或图片 | 继续当前会话；首次按项目最近更新选择可输入会话 |
| `会话列表` | 显示最近的会话与序号 |
| `切换 2` | 切到最近一次成功发送列表中的第 2 个会话 |
| `切换 完整名称` | 切到唯一同名会话；重名请用序号 |
| `当前会话` | 查看当前项目和会话 |
| `新建任务 名称` | 新建并选中会话，不自动开始执行 |
| `CABC123 答案` | 回答对应编号的问题；选项也可用序号 |

图片附带文字按用户内容处理，不触发会话管理命令。切换不取消原任务，也不会根据聊天间隔自动新建会话。支持普通微信 DM；不作为群聊机器人使用。

## 图片与问题通知

图片支持 PNG/JPEG/WebP/GIF，每条最多 9 张、单张最多 20 MiB。Hermes 下载图片后，桥接保存私有副本并以 `localImage` 输入 Codex。缓存清理不影响已收到的副本。

微信管理过的会话，其最终结果中的 `![说明](/绝对路径.png)`、`sandbox:/...`、`file:///...`、独立 `MEDIA: /绝对路径.png`，以及原生 `imageGeneration.savedPath`，会自动回传图片。远程图片 URL 暂不下载。

在目标 Codex 会话内执行下面的命令；保持原 `CODEX_THREAD_ID`：

```bash
# 手工发送图片；同一次发送重试使用同一个 key。
python -m harness.cli send-image --path /absolute/path/to/image.png --key image-001

# 通知一个需要本人回答的问题。
python -m harness.cli ask --key question-001 \
  --question '请补充下一步需要的信息' --option '继续' --option '稍后'
```

以上 `python` 须使用已安装桥接及 Hermes 的环境，也可以使用安装后的 `codex-wechat` 命令。其他桌面会话可用显式命令发送图片，不会自动推送普通结果。

原生 `item/tool/requestUserInput` 通过对应 RPC 请求答复，等待 `serverRequest/resolved`；桌面 `request_user_input_async` 通过只读 rollout 监听镜像，微信回答以用户消息回到原会话，不会直接关闭桌面卡片。原生分支的联调应使用真实原生请求，不能用桌面提问验证替代。

## 配置、状态与边界

配置示例见 [`deploy/config.example.json`](deploy/config.example.json)。

- `project_name`：可选，在微信显示项目名称；默认项目目录名。
- `codex_state_db`：可选，桌面提问的 SQLite 索引。安装时尝试从 `CODEX_HOME` 或 `~/.codex` 选择版本最高的 `state_*.sqlite`；不存在时跳过桌面监听，普通聊天仍可运行。填 `null` 可关闭。
- `thread_ids`：可选，限制后台订阅的会话范围；不作为本机进程的安全授权边界。
- `CODEX_WECHAT_HOME`：可选，改变私有配置和状态目录；设置时 **Hermes 网关与桥接进程须使用相同值**。默认源码目录下 `.local`。

仅接收配置的微信会话与授权身份，还会调用 Hermes 的来源授权校验。微信登录凭据继续由 Hermes 自身管理，不复制到本项目。

私有目录 0700，配置、数据库、图片副本和 Unix socket 使用用户私有权限。不要把 socket 转发为公网接口。本机同一用户的进程可以调用桥接接口；聊天功能沿用 Codex 已配置权限。`.local` 的聊天状态和图片会保留用于续接，需要备份时连同私有配置一起保存。

| 状态 | 含义 |
| --- | --- |
| `sent` | 微信接口确认接受，不代表对方已读 |
| `delivered` | App Server 确认接收编号回答 |
| `queued` | 尚未递交，等待连接或微信会话恢复 |
| `blocked_session` | 微信要求本人发一条消息恢复会话 |
| `uncertain` | 写入结果未确认，不自动重发 |
| `stale` / `needs_review` | 原轮次变化或请求失效，需核对原任务 |

单个会话订阅报错不会断开整条共享连接。断线期间尚未递交的输入等待恢复，并继续接收时选中的会话。已递交但回执不明的输入不会重放。微信上传接口偶尔可能返回 500；仅生成图片不代表微信端已收到。

## 开发验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

测试使用模拟微信和 Codex，不向真实用户发消息。覆盖 Unix socket 插件交接、鉴权、原会话 start/steer、编号问题回传、去重、图片持久化、断线恢复与可移植安装。

真实环境已验证微信图片进入 Codex、文字结果回传及图片接口成功回执；真实平台可用性仍取决于各自账号、连接与版本。运行日志只保留必要状态和错误类型，不记录聊天正文或微信凭据。

## 许可

[MIT](LICENSE)。Hermes 与 Codex 是独立项目，本仓库不分发它们的代码、凭据或用户聊天数据。
