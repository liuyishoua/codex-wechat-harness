# Codex ↔ Hermes 微信桥接

把 **Hermes 已绑定的微信**接到 **现有 Codex App Server 会话**：微信发文字、图片和语音，Codex 在同一任务里处理，再把结果发回微信。支持会话切换、图片双向传递和需要用户回答时的微信通知。

Hermes 管微信登录与收发；本项目只做桥接。不会另起 Hermes 模型任务，不直接轮询微信 getupdates，不替换 Codex 的模型或权限配置。

```text
微信用户 ⇄ Hermes Weixin adapter ⇄ 本地桥接 ⇄ Codex App Server
             登录 / 图片下载       去重 / 会话选择      原会话续接
```

## 支持什么

- 文字、语音转写、纯图片、带文字图片和多图输入；运行中补充，空闲时续接。
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

现在给绑定的机器人发送「help」，然后发一条文字、图片或语音验证真实收发。

### Codex socket

优先使用已有 App Server 的 Unix WebSocket 地址，以续接桌面会话。不同版本和宿主的路径不同，以宿主配置为准。运行 `codex app-server --help` 检查版本：本机验证版本支持 `--listen unix://PATH`。

如果没有运行中的 App Server，可按本机版本支持的方式启动一个：

```bash
mkdir -p "$HOME/.codex"
chmod 700 "$HOME/.codex"
codex app-server --listen "unix://$HOME/.codex/wechat-control.sock"
```

把该路径传给 `--codex-socket`。新启动的 App Server 是否可直接续接某个桌面会话，取决于宿主和 Codex 版本；不要把桥接健康状态当作所有桌面任务都可控制的证明。

支持原生项目 RPC 的主机可以配置 `desktop_project_sync: true`。桥接通过 `project/list` 找到默认目录对应的项目；没有项目时用固定幂等键创建项目。新会话的 `thread/start` 带上 `projectId`，续接的旧会话若尚未归属项目且目录一致，通过 `thread/metadata/update` 补关联。已有项目归属、临时会话和其他目录的会话不会改动；多个项目包含同一目录时不自动选择。微信、QQ 共用主机上的原生项目。桌面仍需刷新项目或会话列表；项目关联本身不保证两个独立 App Server 的实时消息同步。

2026-10-14 已核验 MacBook、Mac mini、Windows 的项目 RPC，并启用微信和 QQ 的上述配置。补关联了 MacBook 的 2 个桥接会话、Mac mini 的 1 个桥接会话；Windows 当前没有桥接管理的会话，沿用已有项目。变更回执保存在私有 `.local/desktop-project-backfill-1791955782.json`，记录变更前的项目归属，可用原生 API 恢复。全套 91 项测试通过。

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
| 普通文字、图片或语音 | 继续当前会话；首次按当前主机最近更新选择可输入会话 |
| `会话列表` | 汇总当前主机全部项目最近 10 个可输入会话，只显示序号、会话名称和当前会话标记 |
| `切换 2` | 按指令收到时已发送的列表切到第 2 个会话；列表 10 分钟有效，新增会话后需刷新 |
| `切换 完整名称` | 切到当前主机唯一同名会话；重名请用序号或完整会话 ID |
| `切换 完整会话ID` | 直接读取并切到该 UUID 对应的可输入会话，不受最近列表范围限制 |
| `当前会话` | 查看当前会话、实际工作目录和完整会话 ID |
| `新建会话 名称` | 在配置的默认目录新建并选中会话，不自动开始执行 |
| `CABC123 答案` | 回答对应编号的问题；选项也可用序号 |

图片附带文字按用户内容处理，不触发会话管理命令。切换不取消原任务，也不会根据聊天间隔自动新建会话。支持普通微信 DM；不作为群聊机器人使用。

会话查询不再按 `project_cwd` 过滤；同名项目位于不同工作目录时也能找到。续接保留会话原有的目录和项目归属，不移动文件或修改默认新建目录。列表只汇总当前主机的会话，各主机及微信、QQ 的选择和列表分别保存。

会话中的 `commentary` 进展消息也会回传，最终结果仍按执行轮次去重。原生 `item/commandExecution/requestApproval` 会显示命令、目录、原因及授权编号；只支持服务端提供的“允许本次”“拒绝本次”或“拒绝并停止本轮”，不提供会话范围或永久授权。回复卡片的序号或完整选项文字后，以原生 `decision` 交回当前执行并等待确认回执。命令无法完整展示时，手机端只提供拒绝选项，需到 Codex 桌面核对。控制连接恢复后，同一审批回调恢复原编号，不重复创建授权问题。

### 多主机接入

在私有 `.local/config.json` 中保留原本机配置，再添加 `hosts`：

```json
{
  "local_host_id": "ubuntu",
  "local_host_name": "Ubuntu",
  "hosts": [{
    "id": "mac-mini",
    "name": "Mac mini",
    "aliases": ["leomac-mini"],
    "codex_socket": "/home/ubuntu/leo-project/codex-wechat/.local/hosts/mac-mini/control.sock",
    "project_cwd": "/Users/leo/leo-project",
    "ssh_target": "leo@100.117.21.117",
    "ssh_identity": "/home/ubuntu/.ssh/id_ed25519",
    "remote_image_dir": "/Users/leo/.codex/wechat-images"
  }]
}
```

这是需要合入原配置的字段，不是完整配置文件。`codex_socket` 是 SSH 在 Ubuntu 上转发出的 Unix socket；远端 App Server 保持运行。SSH 主机密钥必须事先核验，隧道使用 `StrictHostKeyChecking=yes`。部署示例见 `deploy/codex-wechat-mac-mini-tunnel.service`，更换路径和地址后安装启用。

微信发送 `主机列表`、`当前主机`、`切换 macmini`、`切换 macbook`、`切换 windows`、`切换 ubuntu`。也兼容无空格的写法和 `切换主机 名称`。主机别名忽略大小写、空格、横线和下划线；`切换 2` 仍切换当前主机的会话。注册的主机名称优先于同名会话。切换主机要求目标 Codex 控制连接在线，失败保留原主机。

Windows 和 MacBook 同样作为 `hosts` 条目接入。MacBook 的 SSH Unix socket 隧道示例见 `deploy/codex-wechat-macbook-tunnel.service`；实际 SSH 登录名和用户目录可能不同，请分别确认。当前 Windows 主机的 SSH 默认 shell 为 Git Bash，使用现有 Codex 的 `app-server proxy --sock` 透传原始 WebSocket 数据，Ubuntu 上由 `python -m harness.ssh_proxy --host windows` 提供私有 Unix socket，不新建或重启远端 App Server。服务示例见 `deploy/codex-wechat-windows-proxy.service`。

Windows 主机条目增加 `remote_platform: "windows"`、`codex_proxy_socket: "C:/Users/用户名/.codex/app-server-control/app-server-control.sock"`、`project_cwd: "E:/leo-project"`、`remote_python: "D:/anaconda/python.exe"` 和 `remote_image_dir: "C:/Users/用户名/.codex/wechat-images"`，并保留 Ubuntu 侧的 `codex_socket`、`ssh_target` 和 `ssh_identity`。按实际安装路径修改；`remote_python` 必须指向可用 Python，不能使用未安装解释器的 WindowsApps 占位程序。Windows 项目与结果图片支持盘符路径，项目匹配忽略大小写和斜杠差异。其他 Windows SSH shell 需要适配命令转义后才能使用。四台主机分别保存自己的会话、问题和消息队列，只有当前选中主机可以推送消息。

每台主机使用独立数据库保存会话选择、编号列表、问题、输入队列和发送状态；重启保留主机选择。消息在接收时固定目标主机，断线消息恢复后仍交给原主机，不转交当前主机。切换不会中断原任务；只接收当前选中主机的结果、图片、确认问题和状态回复，其他主机的消息留在各自队列，切回后恢复发送，过期或失效问题不再发送。所有文字回复标明主机；回复其他主机的问题编号需要先切回原主机，编号碰撞拒绝递交。已经提交给微信的消息可能在切换后才到达。

切换主机、切换会话和新建会话时，先通过 `thread/unsubscribe` 释放旧会话，再连接目标会话。释放状态持久保存在 `chat_released`，后台轮询和断线重连不会重新占用已释放的会话；旧任务结果通过 `thread/turns/list` 查询，不需要重新连接。发送「断开会话」可释放当前会话，让桌面点击「重试」接续；再次明确发消息或切换回该会话会重新连接。切换失败不提交目标输入；取消订阅与中断任务分别使用不同的原生 API，桥接切换不发送 `turn/interrupt`。未选中的主机不自动订阅会话，两个通道各自保存连接状态。

注意：这里释放的是桥接订阅，不能据此认定跨进程写入锁已立即释放。原生服务在最后一个订阅者离开后仍有闲置保留期；独立终端或另一个 App Server 也可能持有 `thread-writer-locks`。桌面仍提示“已在另一个应用中打开”时，应先核对线程编号和实际持锁进程；只有确认任务已完成、相关后台全部空闲且无待提交输入后，才关闭相应客户端或通过原生 daemon 管理命令释放后台锁。不要删除锁文件、归档历史或对正在执行的任务直接重启后台。

配置 SSH 图片传输后，入站图片先通过 SSH 保存到远端私有目录，再传入远端 `localImage`；结果图片通过 SSH 下载、验证格式和大小后由 Ubuntu 微信通道发送。只支持已有图片类型与 20 MiB 上限。

远端桥接只订阅选中的和微信管理的会话，不自动订阅全部已加载任务。共享 App Server 能恢复的历史会话与桌面独立 App Server 中正在执行的任务有区别；接入共享接口不代表接管桌面正在运行的任务。远端原生 `requestUserInput` 可回到微信；桌面专用 `request_user_input_async` 的 rollout 监听当前仅支持本机。

`health` 返回每台主机的控制连接状态、当前项目和会话；“Codex 已连接”表示控制接口可用，不能作为独立桌面任务可接管的证明。未配置 `hosts` 时保留原单主机行为。Hermes 继续只在 Ubuntu 上登录和收发微信。

## QQ 入口

Hermes 已启用 QQ 后，同一个 `codex-wechat-harness` 插件也能把 QQ 私聊交给这套桥接。QQ 使用私有 `.local/channels/qqbot/config.json`，以微信配置为基础保留四台主机的连接字段，再设 `platform: "qqbot"`、`chat_id` 和 `allowed_users` 为已绑定 QQ 私聊的 OpenID（不是数字 QQ 号）。设 `gateway_send_socket` 为该目录下 `outbound.sock`，并设 `managed_threads_only: true`，只监听此入口选择或管理的会话。QQ 不另存机器人密钥，也不创建第二个 QQ 登录连接。

运行 `deploy/codex-qq-harness.service` 中的独立桥接服务。插件需通过 Hermes 已有控制接口热加载。QQ 首条授权私聊会建立私有发送 socket，复用 Hermes 正在运行的 QQ adapter 和最新消息回复上下文；QQ 主动消息或回复上下文受限时，以实际发送回执为准。尚无发送上下文的通知留在队列，收到授权新消息后恢复；回执不明的消息不自动重发。

QQ 和微信均支持 `help`、`帮助`、`主机列表`、`当前主机`、`切换 macmini`、`切换 macbook`、`切换 windows`、`切换 ubuntu`、`会话列表`、`当前会话`、`新建会话 名称`、会话切换及编号回答。支持文字和图片；QQ 已转写语音去掉 adapter 的 `[Voice]` 标记后走同样的命令路由，识别失败不执行命令。结果、图片和确认问题回传到对应通道，两个入口分别保存主机选择、会话选择、编号列表、问题、输入去重和发送队列，均只推送本入口当前选中主机的消息。未绑定的 QQ 用户保留 Hermes 原有路由；已绑定入口故障时不会自动转给 Hermes 模型执行。

使用 CLI 检查 QQ 状态时，设置 `CODEX_WECHAT_HOME=/绝对路径/codex-wechat/.local/channels/qqbot` 后运行 `python -m harness.cli health`。`health` 返回 `platform` 和通用 `channel_needs_message`；旧 `wechat_needs_message` 字段作为兼容别名保留。

## 语音输入

微信语音会先调用 Hermes 已配置的语音转文字流程，再把转写文字送入当前 Codex 会话。沿用 Hermes 的 STT 提供商、本地回退和事件转写缓存，支持微信 `.silk`；无需另配一套识别凭据。语音识别出的会话命令和编号回答与文字输入使用同一路由。

此功能要求 Hermes 提供 `_transcribe_pending_audio_event_once`，且语音识别已启用、识别服务可用。识别失败、超时、空白或多段识别不完整时，会提示重发或改用文字，不执行部分内容，也不会把请求转给 Hermes 模型。语音转写后目前回传文字结果和图片，不自动合成语音回复。

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

微信通知在后台独立发送：完成结果先持久化，输入处理、完成事件和会话轮询不等待微信回执。发送默认最多等待 20 秒，可用 `notification_timeout` 配置（最多 60 秒）；超时进入 `uncertain`，不自动重发。重复唤醒发送队列会合并为同一个后台任务。图片与提问接口返回排队状态，实际结果以状态记录为准。微信会话失效不代表 Codex 在等待业务决策，应继续所有不依赖用户答案的工作；收到新的授权微信消息后只补发尚未发送的通知。

`outbound_queue_limit: 10` 将每个通道的待发通知限制为最近 10 条。微信和 QQ 分别配置、分别计数；同一通道四台主机的文字、图片与确认提示共用限额，超过后将最旧的通知标为 `discarded`，保留去重记录，恢复连接也不补发。丢弃确认提示不代表批准或取消原生请求；输入任务和会话历史不受限额影响。发送中的请求不因裁剪而重发。

## 开发验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

测试使用模拟微信和 Codex，不向真实用户发消息。覆盖 Unix socket 插件交接、鉴权、原会话 start/steer、编号问题回传、去重、图片持久化、断线恢复与可移植安装。

真实环境已验证微信图片进入 Codex、文字结果回传及图片接口成功回执；真实平台可用性仍取决于各自账号、连接与版本。运行日志只保留必要状态和错误类型，不记录聊天正文或微信凭据。

## 许可

[MIT](LICENSE)。Hermes 与 Codex 是独立项目，本仓库不分发它们的代码、凭据或用户聊天数据。
