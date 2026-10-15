# feishu-gpu-board

[English](README.md) | **简体中文**

**在飞书 / Lark 里给团队一块实时的「哪些 GPU 卡空着」看板。**

每台 GPU 机器上的每张卡，都会变成飞书多维表格里的一行。表里写着这张卡是空闲还是占用、显存和利用率、上面跑着哪些进程。如果这张卡在跑推理服务，还会写出模型名和调用它的 `ip:端口`。看板每分钟刷新一次。团队本来就天天用飞书，所以找空卡不用再 SSH 上机器敲 `nvidia-smi`，也不用在群里问。

![架构图](docs/architecture.zh-CN.svg)

- **不用部署任何服务。** 没有服务器、数据库或网页。每台 GPU 机器上只跑三个纯标准库的 Python 小进程，看板就是你已经有的一张多维表格。
- **没有 cron / systemd 也能跑。** 它是为 Kubernetes 里的 GPU 容器写的。一个很小的守护脚本负责把进程拉起来，你笔记本上可选的看门狗负责在容器重启后自动重新部署。
- **在 GPU 机器上只读。** 采集进程只调用 `nvidia-smi`、`ps` 和 `docker ps`，并读取 `/proc`。不发信号、不杀进程、不 attach 任何进程，也不持有任何凭证。
- **知道谁在哪儿提供什么服务。** 它会从每个 GPU worker 进程一路往上找到启动它的命令：`vllm serve` 和 SGLang（完整解析），以及 lmdeploy、`trtllm-serve`、TGI（尽力解析）。模型名和该调用的地址都从这条命令里读出来。
- **离线检测。** 一台机器失联后，写同一张表的其它机器会在大约 4 分钟内把它的卡标成 **Stale**。如果一张表只有这一台机器，它没法给自己打标；这时看 `更新时间` 就知道数据有多旧，看门狗也会把它重新拉起来。
- **预定。** 在 `预定人` 里填上名字，这张卡就显示 **Reserved**，直到预定时段结束；但如果卡上真有任务在跑，仍显示 Busy。

---

## 目录

- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [让 AI 助手帮你部署](#让-ai-助手帮你部署)
- [看板长什么样](#看板长什么样)
- [配置](#配置)
- [日常运维](#日常运维)
- [安全设计](#安全设计)
- [已知限制](#已知限制)
- [开发](#开发)

## 工作原理

每台 GPU 机器上跑同样的三个进程，由 `supervise.sh` 保活：

| 进程 | 做什么 | 网络 | 凭证 |
|---|---|---|---|
| `board/node_agent.py` | 每 60 秒：`nvidia-smi` + 沿 `/proc` 回溯父进程 → `nodes/<label>.json` | 无 | 无 |
| `board/cpu_agent.py` | 每 60 秒：负载、内存、磁盘、IO、任务进程 → `cpu_snaps/<label>.json` | 无 | 无 |
| `board/master.py` | 每 60 秒：读快照和表格，算出每张卡的状态，一次批量写回 | 飞书 API（HTTPS） | 应用密钥 |

同一种 GPU 的机器通常共用一张表（`H200`、`B200`……）。每台机器的 master 只写自己那几行。别的机器的行如果超过 `STALE_SECONDS`（3 分钟）没人更新，它会在下一轮把那些行标成 **Stale**。所以没有哪台机器依赖别的机器，一台挂了，兄弟机器会替它报出来。

部署是从你的笔记本通过 SSH 完成的（`deploy/deploy.sh`）。它只发三个 `.py` 文件和两个环境变量文件过去，从不把整个仓库放到机器上。可选的 `deploy/watchdog.sh` 每 5 分钟检查一遍所有机器，发现哪台的进程没了就重新部署。

完整设计，包括状态规则、服务模型识别、飞书 API 配额策略，以及每个取舍背后的原因，见 **[docs/architecture.zh-CN.md](docs/architecture.zh-CN.md)**。

## 快速开始

你需要：

- **GPU 机器**（物理机、虚拟机或容器都行）：能免交互 `ssh` 登录，装有 `nvidia-smi` 和 `python3` ≥ 3.7，并且能出网访问 `open.feishu.cn`（或 `open.larksuite.com`）。用什么用户登录都行，不需要 root。
- **一个飞书企业自建应用**，开通 `bitable:app` 权限；以及一个这个应用能编辑的**多维表格**。见 [飞书应用和多维表格](#1-飞书应用和多维表格)。
- **一台笔记本**（macOS 或 Linux），有 `bash`、`ssh`、`gzip`，以及建表时用一次的 `python3`。

### 1. 飞书应用和多维表格

1. 打开[飞书开发者后台](https://open.feishu.cn/app)（Lark 用 [open.larksuite.com/app](https://open.larksuite.com/app)），创建一个**企业自建应用**。在**权限管理**里添加 `bitable:app`，然后创建并发布一个版本，让权限生效。
2. 在**凭证与基础信息**里复制 **App ID**（`cli_…`）和 **App Secret**。
3. 新建一个多维表格（或用现有的）。在多维表格右上角 `…` 菜单里点**添加文档应用**，把你的应用加进来，给**可编辑**权限。
4. 从多维表格的网址里复制 Base token：`https://<租户>.feishu.cn/base/`**`<BASE_TOKEN>`**`?table=…`

### 2. 配置

```bash
git clone https://github.com/mjykxz17/feishu-gpu-board && cd feishu-gpu-board
cp deploy/targets.example.conf deploy/targets.conf      # 已在 .gitignore 里
bash deploy/deploy.sh secret       # 隐藏输入应用密钥 -> ~/.config/gpu-board/secret，权限 600
```

编辑 `deploy/targets.conf`：填上 `FEISHU_APP_ID` 和 `FEISHU_BASE_TOKEN`，然后把示例里的 `target` 行**换成**你自己的，每台机器一行：

```bash
#      名字    ssh 主机           节点名       GPU 表 id（第 3 步之前先写 PENDING）
target h200-a  gpu-h200-a         h200-a       PENDING
target b200-a  root@10.0.0.12     b200-a       PENDING
```

### 3. 建表

```bash
bash deploy/deploy.sh setup --gpu-table H200 --gpu-table B200 --cpu-table "CPU 负载"
# -> TABLE gpu H200 tblXXXX…   TABLE gpu B200 tblYYYY…   TABLE cpu CPU 负载 tblZZZZ…
```

把 GPU 表的 id 填进对应的 `target` 行，CPU 表的 id 填进 `FEISHU_CPU_TABLE_ID`。`setup` 可以放心重复跑：已经存在的表只会补上缺的列。

> 如果你的笔记本连不上飞书 API（比如公司代理拦了 TLS），就借一台 GPU 机器去跑：`deploy.sh setup --via <ssh 主机> …`。它会把 `setup_tables.py` 作为临时文件发到那台机器上执行，跑完就删掉。

### 4. 检查、部署、验证

```bash
bash deploy/deploy.sh all preflight   # ssh、GPU、python、能否连飞书（只会创建 ~/gpu-board 目录）
bash deploy/deploy.sh all             # 在每台机器上安装并启动
bash deploy/deploy.sh all status      # 进程、快照新鲜度、master 日志最后几行
```

部署成功会打印 `[h200-a] OK supervisor=…`，接着是 `feishu: pushed nodes=1 (fresh 1) updated=0 created=8`。打开多维表格，每张卡已经各占一行了。

### 5.（可选）容器重启后自动恢复

```bash
bash deploy/install_watchdog.sh       # macOS：装 LaunchAgent，每 5 分钟跑一次；Linux：打印一行 crontab
```

看门狗只在它所在的机器开着的时候生效。想要 7×24 小时守护，就把它装在一台常开、能 SSH 到 GPU 机器的服务器上。

## 让 AI 助手帮你部署

仓库自带一个 [Agent Skill](https://agentskills.io)：[`skills/feishu-gpu-board/SKILL.md`](skills/feishu-gpu-board/SKILL.md)。它把整件事教给编程助手：该问你要哪些信息、预检、建表、部署、验证、排障，以及安全规矩（密钥怎么处理、绝不碰别人的进程）。

**Claude Code**（这个仓库同时也是一个插件市场）：

```text
/plugin marketplace add mjykxz17/feishu-gpu-board
/plugin install feishu-gpu-board@feishu-gpu-board
```

然后直接说：*「帮我给 gpu-1 和 gpu-2 两台机器在飞书里建一个 GPU 看板」*。

**不用插件系统的话**，把 skill 文件夹复制到 `~/.claude/skills/feishu-gpu-board/`（或项目里的 `.claude/skills/`）。其它助手（Codex、Cursor 等）直接让它读这份 SKILL.md 就行；本地没有这个仓库时，skill 会自己 clone。

## 看板长什么样

**GPU 表**：每张卡一行。master 写这些列：

| 列 | 含义 |
|---|---|
| `节点` | `targets.conf` 里的节点名（主列） |
| `卡号` | 卡的编号 0…N |
| `状态` | **Free** · **Busy** · **Reserved** · **Stale** |
| `显存` | `已用 / 总量 GB` |
| `利用率` | GPU 利用率 % |
| `占用进程` | 卡上每个计算进程的 `进程名:pid` |
| `服务模型` | 如果这张卡属于某个推理服务，写模型名，否则留空 |
| `服务端点` | 调用它的 `ip:端口`；如果它只监听本机回环地址，写成 `127.0.0.1:端口 (local only)` |
| `更新时间` | 最后一次真正看到这张卡的时间。机器变 Stale 后它就不再变 |

`负责人`、`备注` 和预定相关的 `预定人` / `预定开始` / `预定结束` 归人来填。master 从不覆盖这些列，只会清掉已经过期的预定。

**状态规则**（从上到下优先级递减）：

1. **Stale**：这张卡的快照比 `STALE_SECONDS`（180 秒）旧，或者它所在的机器不再上报。
2. **Busy**：卡上有任何计算进程，或者显存占用 ≥ `FREE_MEM_MB`（2048 MB）。
3. **Reserved**：填了 `预定人`，并且现在处在预定时段内。没填开始时间的，master 会把第一次看到这条预定的时刻写进 `预定开始`。没填结束时间的，预定到当天 23:59:59 结束，按 `BOARD_UTC_OFFSET` 时区算（默认是你部署时笔记本的时区）。
4. **Free**：其它情况。卡空闲而预定已过期的，预定会被清掉。

**视图**（`setup` 自动建）：`空闲 Free now`、`空转占用 Held but idle`（Busy 但利用率 0%，即被进程占着却没在算）、`离线 Offline`。想要图表的话，可以在上面自己加一个仪表盘，这一步需要手动做。

**CPU 表**（可选）：每台机器一行，包括 `状态`（Active / Idle / Stale）、负载和 1 小时峰值、内存 %、磁盘 %、IO %、容器数、任务进程数，以及一段大白话摘要（`· vLLM 推理服务 ×2`）。

## 配置

所有配置都在 `deploy/targets.conf` 里（bash 语法，已在 .gitignore 里）：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `FEISHU_APP_ID` | — | 应用 id `cli_…` |
| `FEISHU_BASE_TOKEN` | — | 网址里的 Base token |
| `FEISHU_BASE_URL` | `https://open.feishu.cn` | Lark 用 `https://open.larksuite.com` |
| `FEISHU_CPU_TABLE_ID` | 空 | CPU 表；留空 = 不写 CPU 表 |
| `SECRET_FILE` | `~/.config/gpu-board/secret` | 一行 `FEISHU_APP_SECRET=…`，权限 600 |
| `REMOTE_DIR` | `gpu-board` | 每台机器上的安装目录（相对路径 = 在 `$HOME` 下） |
| `PYTHON` | `python3` | 机器上用的 Python |
| `CYCLE_SECONDS` | `60` | 采集和 master 的刷新周期 |
| `CPU_PUSH_SECONDS` | `300` | CPU 表的刷新周期 |
| `STALE_SECONDS` | `180` | 快照超过这个秒数就算 Stale。应 ≥ 2 × `CYCLE_SECONDS` + 60 |
| `FRESH` | 2 × `CYCLE_SECONDS` + 180 | 快照或日志比这个旧时，`check` 判定机器为 `down` |
| `BOARD_UTC_OFFSET` | 本机时区 | 小时数（如 `8`），预定按这个时区算「当天结束」 |
| `FREE_MEM_MB` | `2048` | 显存低于这个值（且没有进程）算 Free |
| `SERVE_IP_PROBE` | `1.1.1.1` | 服务端点里的 IP 取「通往这个地址的网卡」的地址；多网卡机器请填集群内网里的一个地址 |
| `SERVE_PATTERNS` | 空 | 逗号分隔的启动命令特征，**追加**到内置列表（要带启动动词，比如 `mycorp-serve`） |
| `WORKLOAD_PATTERNS` | 空 | `ps` 子串，**追加**到 CPU 表内置的任务进程列表 |
| `FEISHU_PREFER_V3`、`SWEEP_SECONDS`、`DETAIL_SECONDS`、`CPU_STALE_SECONDS` | 不设 | master 的高级参数，设了才透传；见[架构文档](docs/architecture.zh-CN.md#飞书-api-和配额) |
| `SSH_OPTS` | 空 | 额外的 ssh 参数，比如 `-i ~/.ssh/gpu_key -p 2222` |
| `target 名字 主机 节点名 表id` | — | 每台机器一行 |

**API 用量**：每台机器的 master 每轮大约 2 次调用。60 秒一轮的话，一个月约 9 万次，CPU 表按 300 秒一轮再加约 1.7 万次。这是**每台机器**的量，N 台就是 N 倍。如果你所在租户的飞书套餐每月 OpenAPI 额度不高，就把 `CYCLE_SECONDS` 调大，并把 `STALE_SECONDS` 一起调大。万一额度还是用完了，master 会切换到 base v3 API，继续保持状态是最新的，而不是整块看板冻住（见[架构文档](docs/architecture.zh-CN.md#飞书-api-和配额)）。

## 日常运维

```bash
deploy/deploy.sh targets              # 列出所有机器
deploy/deploy.sh <t|all> status       # 在跑什么、快照新鲜度、master 日志
deploy/deploy.sh <t|all> check        # healthy / down / unreachable（单台时退出码 0/1/2）
deploy/deploy.sh <t|all> print        # 采集进程此刻看到的每张卡
deploy/deploy.sh <t|all>              # 重新部署（升级也是这条）
deploy/deploy.sh <t|all> stop         # 停止，保留文件
deploy/deploy.sh <t|all> uninstall    # 停止，并删掉它创建的文件
```

机器上的东西都在 `~/gpu-board/` 里：`node_agent.log`、`cpu_agent.log`、`master.log`（会自动裁剪）、`nodes/`、`cpu_snaps/`、`agent.env`、`master.env`（0600）和 `supervise.pid`。`deploy.sh <t> print` 会用那台机器自己的配置跑一次采集，把每张卡打印出来。

| 现象 | 可能原因 → 处理 |
|---|---|
| 某台机器所有卡都是 **Stale** | 机器（容器）重启了或进程挂了 → `deploy.sh <t>`，或装上看门狗 |
| 某台机器的 `更新时间` 很旧，但卡没变 Stale | 它是这张表上唯一的机器，而且挂了，没人能替它打标 → 同上 |
| `master.log` 里有 `token error` | App ID 或密钥填错了 |
| API 报 forbidden / 没权限 | 应用没被加进多维表格、或没给编辑权限；或者权限所在的版本还没发布 |
| 有列在界面上被删了或改了名 | 再跑一次 `deploy.sh setup …` 把列补回来，然后重新部署（master 只在启动时读一次列名）。如果缺的是 `节点` 或 `卡号`，master 会停止写入并在日志里说明，不会瞎猜 |
| 日志里有 `quota dead` | 每月 API 额度用完了，master 已经在走 v3 兜底。把 `CYCLE_SECONDS` 和 `STALE_SECONDS` 调大 |
| 服务端点 IP 不对 | 多网卡机器：把 `SERVE_IP_PROBE` 设成大家调用时所在网络里的一个地址 |
| 卡明明在跑服务，模型/端点却是空的 | 这种启动方式没认出来；把它的启动命令加进 `SERVE_PATTERNS`（见[已知限制](#已知限制)） |

## 安全设计

- **一个密钥，每台机器只存一处。** 应用密钥从你本地的 `SECRET_FILE` 读出，夹在部署脚本里通过 SSH 的标准输入发过去，从不出现在命令行上。到了机器上只写进 `master.env`（权限 600），也只有 `master.py` 会读。两个采集进程用的 `agent.env` 里没有密钥。
- **在 GPU 机器上权限最小。** 采集进程只读：调 `nvidia-smi` 查询，读 `/proc/<pid>/cmdline` 和 `/proc/<pid>/status`，跑 `ps` 和 `docker ps -q`。不发信号也不杀任何进程。这套工具唯一会停掉的只有它自己的进程，而且停之前会先核对命令行。
- **看板上不会出现完整命令行。** `占用进程` 只显示 `进程名:pid`，服务模型取自 `--served-model-name` / `--model`。写在命令行里的 API key（比如 `--api-key`）永远到不了表格里。
- **应用权限有限。** 应用只需要 `bitable:app`，而且只能看到明确把它加进去的多维表格。

## 已知限制

- **只支持 NVIDIA**：依赖 `nvidia-smi`。
- **用配置文件启动的服务**：用 `sglang serve --config server.yaml` 启动时，端口和模型都写在 YAML 里。模型列会是空的，端点显示的是引擎默认端口，可能不对。包了一层 vLLM 的自定义 Python 服务完全识别不出来，两列都是空的。
- **其它引擎是尽力解析**：lmdeploy、`trtllm-serve` 和 TGI（`text-generation-launcher`）能识别，常用的模型、主机、端口参数也会读。真正在线上验证过的是 vLLM 和 SGLang。
- **表格建议控制在几千行以内**：读取有分页，但每轮都会读整张表，所以建议一种 GPU 一张表，别把所有东西塞进一张巨表。
- **跨容器的进程**：如果机器上还跑着别的容器，`nvidia-smi` 报的 PID 可能来自另一个 PID 命名空间。卡仍然会正确显示 Busy，但回溯父进程可能找不到服务。
- **看门狗跑在客户端**：只有运行它的机器开着、能 SSH 进去时，它才能把机器救回来。不想依赖看门狗的话：把 `REMOTE_DIR` 放在持久卷上部署一次，然后把下面这行加进容器的启动命令。它必须写 `supervise.pid`、必须用绝对路径，否则 `check` / `stop` 认不出这套进程：
  `setsid nohup "$HOME/gpu-board/supervise.sh" >/dev/null 2>&1 </dev/null & echo $! > "$HOME/gpu-board/supervise.pid"`
- **列名是中文**，因为这块看板本来就是给飞书团队用的。列名是 `master.py` 和表格之间的约定；要改名，得在 master 的 `FMAP` / `CPU_FMAP` 和 `setup_tables.py` 里一起改。

## 开发

```bash
python3 -m pip install pytest
python3 -m pytest -q            # 完全离线：不需要 GPU、ssh 或飞书
```

目录结构：

```
board/       node_agent.py · cpu_agent.py · master.py · setup_tables.py   （纯标准库）
deploy/      deploy.sh · watchdog.sh · install_watchdog.sh · targets.example.conf
skills/      feishu-gpu-board/SKILL.md      给 AI 助手用的 skill
docs/        architecture.md · architecture.zh-CN.md · architecture.svg · architecture.zh-CN.svg
tests/       pytest 测试
```

## 许可证

[MIT](LICENSE)
