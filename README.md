# Bangumi → Mikan → 光鸭 → Jellyfin

`bmguangya` 将 Bangumi 收藏状态、Mikan RSS、光鸭离线任务、AList 文件树与 Jellyfin STRM 媒体库连接成一条可重复运行的本地自动化流程。整理、匹配和重复处理由仓库内的确定性 Python 规则完成，运行时不需要 AI，也不需要逐个文件手工编排。规则无法唯一确认的内容会留在原处并写入报告，供管理员调整配置或审核；它不会承诺自动识别所有资源。

处理链路如下：

1. 从 Bangumi 读取收藏状态和集数元数据，从已挂载光鸭的 AList 文件树读取当前目录。只有完整扫描才会替换最近一次成功库存；失败时保留旧库存并写入失败记录。可在 `inventory.cloud_source` 选择 `alist` 文件 API 或 `guangya_direct` 光鸭目录 API。
2. 根据新鲜完整库存计算可确认的缺集，并按 Bangumi 收藏状态优先级从 Mikan RSS 匹配简体中文发布。默认跟踪全部收藏类型，默认下载顺序为“在看、想看、看过”；可通过配置缩小范围。DMHY 备用源尚未实现。
3. 只对已确认缺集提交光鸭任务。单集选择和合集拆分必须能把种子内视频逐一准确映射到已知缺集；无法确认的候选会跳过。合集拆分配置为 `max_batch_episode_span`，没有 `allow_batch_torrent_splitting` 这个配置键。带外挂字幕的多文件种子目前不会提交，以免丢失字幕。
4. 先按实际类型处理视频名包装目录：例如真实目录 `EP01.mkv` 会改为 `EP01.mkv [资源包]`，为真正的视频文件释放规范文件名；普通视频文件不受这一步影响。随后按 Bangumi 分类整理已匹配目录，根据完整库存和唯一集号证据归位、规范视频文件名并保留实际视频扩展名。只对与视频同目录且文件主名完全相同的字幕文件进行配对移动。未配对字幕、冲突、未知集号、重复映射和无法匹配的文件会保留并写入报告。不会删除光鸭文件或目录，也不会覆盖现有目标。
5. 从唯一匹配的真实视频生成 `EPxx.strm`，并为唯一 Bangumi 匹配写入带 Bangumi ID 的 `tvshow.nfo`。默认排除 `未标记/待核对-*` 隔离目录及其所有后代；apply 时旧的本机 STRM/本程序 Bangumi NFO 会归档，避免重复包进入 Jellyfin。GUI 库存树仍展示这些真实云端内容。普通未匹配目录仍可以生成播放索引，但不会附加虚构的 Bangumi ID。未知集号不会被猜成普通集；Bangumi 已知 12 集而文件名是 `EP22` 时，不会把它伪装成该条目的普通第 22 集。OVA/SP 等可确认的特殊集单独保留。
6. 向 Jellyfin 发送媒体库刷新请求。Jellyfin 需要由管理员安装、配置兼容当前 Jellyfin 版本的 Bangumi 元数据插件；HTTP 2xx 只表示刷新请求已接受，不代表插件已完成刮削。
7. 在 VidHub 中手动添加 Jellyfin 服务器并使用 Jellyfin 账号登录。VidHub 是播放客户端，不由本程序配置；客户端能否播放还取决于 Jellyfin 与 AList WebDAV 地址对客户端可达。

自动化面向可由规则确认的常见场景，不替代人工审核。新下载候选按发布版本和候选评分选择；已有文件只有在同一条目、同一集的 GCID 与正文件大小完全一致时才收纳多余副本。若同集存在不同内容，每种内容各保留一份，并记录版本差异，不自动判断哪种画质更好。目标冲突、重复集号、未知集、覆盖范围不可靠或云端证据不一致时会阻止对应操作。`reconcile.organize_unassigned` 默认关闭；启用全树整理前应先生成并审阅 dry-run 报告。

Bangumi 的 [`ep` 与 `sort`](https://raw.githubusercontent.com/bangumi/api/master/open-api/v0.yaml) 可以不同：例如本条目第 1 集的 `ep=1`，跨季排序为 `sort=13`。库存使用完整、唯一且连续的元数据建立双编号映射，保留文件原始集号及对应 Bangumi episode ID；明确使用季内编号的目录会对应到规范的 `sort` 集号。编号域重叠、混用或元数据不可靠时标为覆盖未知，停止该条目的自动补缺。单集来源选择和提交前查重使用同一映射；需要转换编号的合集目前会保守跳过并记录原因，普通同编号合集仍可按缺集拆分。

## 部署前提

- Linux 主机，Python 3.11 或 3.12，systemd（若要使用仓库提供的定时服务）。
- 已登录并配置好的 Bangumi 账号/API access token。
- 已安装 AList，且 AList 中的光鸭存储已成功挂载。AList 负责持有和刷新光鸭凭据；本程序不会自行轮换 AList 中的 refresh token。
- 已运行的 Jellyfin。Jellyfin 必须能够读取配置中的媒体目录；VidHub 所在设备必须能够访问 Jellyfin 和 STRM 指向的 AList 地址。
- 一份仅本机可读、权限严格的运行配置。不要把生产凭据提交到 Git、CI 日志或公开报告。

`requirements.txt` 仅包含 Python `requests` 运行依赖。安装、Python 单元测试和字节码编译不需要 Bangumi、AList、光鸭、Jellyfin 的凭据或业务 API 调用。

## 安装与配置

以下步骤使用仓库 systemd 单元的默认安装目录 `/opt/bmguangya`、配置目录 `/etc/bmguangya` 和状态目录 `/var/lib/bmguangya`。如果选择不同路径，需要同步修改 systemd 单元中的 `WorkingDirectory`、`ExecStart`、`ReadWritePaths`、`ReadOnlyPaths` 和 monitor `--state-dir`；不要依赖脚本自动猜测凭据或容器路径。

```bash
sudo install -d -m 0750 /opt/bmguangya /etc/bmguangya /var/lib/bmguangya
sudo chown "$USER" /opt/bmguangya
git clone https://github.com/peppa486/bmguangya.git /opt/bmguangya
cd /opt/bmguangya
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sudo install -o root -g root -m 0600 config/runtime.example.json /etc/bmguangya/config.json
sudoedit /etc/bmguangya/config.json
```

先按实际安装环境改好 `config.json`：

| 配置项 | 部署者需要填写/核对 |
| --- | --- |
| `bangumi.username`, `bangumi.access_token` | Bangumi 用户名和 access token。 |
| `alist.api_base`, `alist.base_url` | AList API 地址。若服务同机，优先使用 loopback，例如 `http://127.0.0.1:5244`。 |
| `alist.public_url` | Jellyfin 和播放客户端能够访问的 AList WebDAV/公开入口；优先使用 HTTPS，例如 `https://alist.example.com`。它会写入 STRM 内容。 |
| `alist.root`, `alist.stream_mount` | AList 中实际挂载路径和其 WebDAV URL 路径。`root` 应指向光鸭里要管理的媒体目录；`stream_mount` 必须对应 AList 的挂载名。将示例中的 `/guangya/Anime`、`guangya` 替换为自己的挂载与目录。 |
| `alist.token` | AList 管理/API token，用于目录扫描、云端整理及让 AList 刷新光鸭挂载。若配置 `alist.database_path`，过期的光鸭 access token 会通过 AList 刷新；没有该 token 时，失效的光鸭 token 无法自动恢复。 |
| `alist.username`, `alist.password` | 专用于 STRM WebDAV 读取的 AList 账号。建议使用只读且仅能访问目标媒体路径的账号。STRM URL 中包含该账号凭据；限制媒体目录与生成文件的读取权限，不要公开分享 STRM 文件。 |
| `alist.database_path`, `alist.guangya_mount_path` | 推荐的光鸭凭据来源：指向 AList SQLite 数据库和数据库中配置的光鸭挂载点。程序以 SQLite `mode=ro` 读取存储凭据；数据库路径因安装方式而异，需现场确认并确保服务进程可读。刷新仍通过 AList API 完成。 |
| `guangya.config_path` | 可选的静态凭据 JSON 路径，格式为 `{"guangya":{"access_token":"…","device_id":"…"}}`。程序只读取该文件，不会续期或改写；过期时须由管理员更新。生产部署优先使用 AList 数据库只读路径。 |
| `guangya.readonly_request_timeout_seconds` | 光鸭只读 API 请求的读取超时，默认 45 秒，范围 1–120 秒；连接超时最多 10 秒。遇到连接错误、408/429/5xx 等暂时错误可按 `readonly_max_attempts` 有界重试。 |
| `guangya.readonly_max_attempts` | 只读请求最大尝试次数，默认 2，范围 1–4。创建、移动、重命名等写请求始终只尝试一次，避免超时后重复产生云端副作用。 |
| `guangya.cloud_task_scan_timeout_seconds` | 全量扫描光鸭任务列表的总时限，默认 900 秒，范围 1–3600；超时会拒绝使用部分游标结果。进度日志只记录聚合页数、数量和耗时，不记录 URL 或游标。 |
| `mikan.subject_overrides` | 可选的手工别名映射，只配置确实需要覆盖的 Bangumi subject ID，例如 `{"123456":{"bangumi_id":"7890","title":"Mikan 上的精确标题"}}`。程序会用 Mikan 搜索结果核对 ID 与标题，并继续执行季/格式冲突检查；不匹配时跳过该条目。 |
| `max_new_cloud_tasks_per_run`, `daily_cloud_task_quota` | 分别限制每轮新建任务数和每日任务数；每日值的代码硬上限为 1000。计数单位是云端任务，合集任务占一个额度。 |
| `max_batch_episode_span` | 合集拆分允许的集数跨度上限；设为 `0` 关闭。代码只提交能逐个对应已知缺集且无外挂字幕的批量任务。 |
| `strm.include_review_directories` | 默认 `false`，不为 `未标记/待核对-*` 及后代创建 STRM/NFO；apply 时把旧的本地生成索引归档。仅在明确希望这些隔离目录也进入 Jellyfin 时设为 `true`。 |
| `media_root`, `archive_root` | Jellyfin 实际扫描的 STRM 媒体目录和本地旧索引归档目录。二者必须分离；`media_root` 需要由 Jellyfin 可读，systemd 的写入许可也要包含它。 |
| `jellyfin.base_url`, `jellyfin.token` | Jellyfin API 地址及专用 API token。建议创建权限最小的专用 token。若 token 留空，代码可以从 `jellyfin.db_path` 以只读方式读取 `jellyfin.username` 最近活动会话的 token。 |
| `inventory.cloud_source` | `guangya_direct` 直接分页扫描光鸭目录，或 `alist` 通过 AList 文件 API 扫描。AList 挂载仍是凭据刷新和 STRM 播放所需依赖。 |
| `reconcile.organize_unassigned` | 示例默认 `false`，先保守运行。用户已授权完整整理时可以设为 `true`，把可安全识别的未归类内容移动至“未标记/待核对”；不确定内容仍会留在原位。务必先看 dry-run 报告。 |
| `reconcile.normalize_video_named_directories` | 默认 `true`。仅对唯一匹配番剧下、实际类型为目录且名称以视频扩展名结尾的直接子目录追加 `[资源包]`，可带 `(N)` 后缀；改名前核对完整子树，普通视频不改名。同名文件与目录造成路径歧义时跳过。已确认的目录重命名会更新中间库存路径，保留原始扫描时间戳，不把路径维护伪装成新的全盘扫描。 |
| `reconcile.collapse_verified_duplicate_directories` | 默认 `false`，且需要 `organize_unassigned=true`。当一个子目录中的全部视频均为已证明相同内容的重复件、保留件全在目录外时，可整包收纳，保留字幕及辅助文件。移动前递归核对完整目录清单；任何新增、缺失或变化都会阻止整包移动。 |

样例的 `dry_run` 保持 `true`，用于保护直接调用。仓库流水线单元会显式以 `--apply` 运行下载、整理和 STRM 同步；只有完成下方检查并由部署者启用 timer 后，定时器才会执行这些写操作。

### 手工处理少数 Mikan 别名

通常使用 Bangumi 与 Mikan 的标题/搜索结果自动匹配。确有个别作品别名无法匹配时，可在 `mikan.subject_overrides` 为对应 Bangumi subject ID 提供已核实的 Mikan 结果，例如：

```json
"subject_overrides": {
  "123456": {
    "bangumi_id": "7890",
    "title": "与 Mikan 搜索结果完全一致的标题"
  }
}
```

只配置需要覆盖的条目。subject ID 和 Mikan ID 必须为正整数（或无前导零的正整数字符串），条目只接受 `bangumi_id` 和 `title` 两个字段；标题去除首尾空白后不能为空。程序只读核对 Mikan 官方搜索结果，要求 ID 相同且标题按 Unicode/标点归一化后完全一致，再执行季/格式冲突检查。搜索不可用或无法核实时会跳过该条目，不会退回缓存猜测；无需 AI，也不需要为普通条目逐个填写映射。

### 首次检查

先运行只读库存扫描及各阶段的 dry-run，审阅 `reconcile-report.json` 与 `strm-sync-report.json`：

```bash
sudo /opt/bmguangya/.venv/bin/python /opt/bmguangya/scripts/library_inventory.py \
  --config /etc/bmguangya/config.json --output /var/lib/bmguangya/library.json
sudo /opt/bmguangya/.venv/bin/python /opt/bmguangya/scripts/bangumi_cloud_download.py \
  --config /etc/bmguangya/config.json --dry-run
sudo /opt/bmguangya/.venv/bin/python /opt/bmguangya/scripts/normalize_cloud_wrappers.py \
  --config /etc/bmguangya/config.json --inventory /var/lib/bmguangya/library.json \
  --report /var/lib/bmguangya/wrapper-normalization-report.json --dry-run
sudo /opt/bmguangya/.venv/bin/python /opt/bmguangya/scripts/reconcile_cloud_library.py \
  --config /etc/bmguangya/config.json --inventory /var/lib/bmguangya/library.json \
  --report /var/lib/bmguangya/reconcile-report.json --dry-run
sudo /opt/bmguangya/.venv/bin/python /opt/bmguangya/scripts/sync_library_strm.py \
  --config /etc/bmguangya/config.json --inventory /var/lib/bmguangya/library.json \
  --report /var/lib/bmguangya/strm-sync-report.json --dry-run
```

库存扫描只读读取 Bangumi、AList/光鸭并写入本地库存、进度与失败报告；其间可能请求 AList 刷新光鸭挂载。扫描会保留同轮成功的目录，只在末尾重试失败目录；`inventory.directory_max_attempts` 默认 3，耗尽后仍拒绝发布不完整库存。下载 dry-run 会读取 Bangumi、Mikan 与完整库存，但不会提交云端任务或更新下载状态，也不证明实时配额充足。整理和 STRM dry-run 会写本地报告，不会移动云端文件、不写媒体目录、也不会请求 Jellyfin。

如果要整理未归类文件，先在配置中将 `reconcile.organize_unassigned` 设为 `true`，重跑库存和整理 dry-run，核对每个目标目录、重复冲突、字幕配对和待核对归档。只有确认报告符合预期后才执行真实流水线。`--apply` 会提交任务、移动/重命名已确认的云端文件，或写入/归档本地 STRM/NFO；不会删除云盘文件。真实流水线各阶段会重新扫描库存，失败时停止后续阶段。

### systemd 定时运行

仓库单元默认假设配置与目录位于上一节列出的安装路径。复制流水线需要的单元（不要启用旧式 `bangumi-mikan-sync.timer`）：

```bash
sudo cp systemd/bmguangya-inventory.service systemd/bmguangya-download.service systemd/bmguangya-wrappers.service \
  systemd/bmguangya-reconcile.service systemd/bmguangya-strm-sync.service \
  systemd/bmguangya-pipeline.service systemd/bmguangya-pipeline.timer \
  systemd/bmguangya-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start --wait bmguangya-pipeline.service
sudo systemctl enable --now bmguangya-pipeline.timer
sudo systemctl enable --now bmguangya-monitor.service
```

流水线包含三次完整库存扫描：下载前、下载后和云端整理后；三次均成功后才同步 STRM。单次库存上限由 `inventory.max_runtime_seconds` 控制（样例为 45 分钟），inventory service 超时为 50 分钟；整个流水线最长 450 分钟。timer 每半小时触发一次，但若前一轮仍运行，全局锁与 systemd 会阻止并行执行，不能把触发间隔当作完成频率。systemd 单元以 root 运行，并限制写入状态目录和 Jellyfin 媒体目录；如果你使用不同路径，需要按最小权限原则调整单元的读写允许路径。

手工运行写入阶段或替换阶段报告前，先停止 `bmguangya-pipeline.timer`，并等待正在执行的流水线和阶段单元结束；停止 timer 不会中断已经启动的任务。确认没有其他写入者后再执行手工命令，完成后恢复 timer。全局锁保护 systemd 流水线，不能替代管理员对独立 CLI 命令的串行管理。

```bash
systemctl list-timers --all bmguangya-pipeline.timer --no-pager
systemctl status bmguangya-pipeline.service bmguangya-pipeline.timer --no-pager
journalctl -u bmguangya-pipeline.service -u bmguangya-inventory.service \
  -u bmguangya-download.service -u bmguangya-reconcile.service \
  -u bmguangya-strm-sync.service -n 200 --no-pager
cat /var/lib/bmguangya/reconcile-report.json
cat /var/lib/bmguangya/strm-sync-report.json
```

## Jellyfin 与 VidHub

1. 在 Jellyfin 建立一个媒体库，路径设为 `media_root`，使 Jellyfin 服务可以读取生成的 `.strm` 与 `tvshow.nfo`。STRM URL 使用配置中的 AList `public_url` 和 WebDAV 用户，所以 Jellyfin 服务及 VidHub 客户端都必须能够解析并访问该 URL。
2. 在 Jellyfin 插件目录安装与你的 Jellyfin 版本兼容的 Bangumi 元数据插件，重启 Jellyfin，并按该插件当前文档配置 Bangumi 数据源/识别设置。生成的 NFO 包含 `uniqueid type="bangumi"` 和 Bangumi 条目 ID。插件是否识别该字段取决于插件版本与设置；检查实际刮削结果，不能仅凭 API 刷新成功判断刮削已完成。
3. 在 VidHub 中添加 Jellyfin 服务器，填写 Jellyfin 的 LAN/VPN/HTTPS 地址并用 Jellyfin 账号登录。确认播放器设备能访问同一 AList WebDAV 地址；如果 STRM 里的地址只在服务器本机可达，VidHub 端会无法播放。

## 只读监控页面

`bmguangya-monitor.service` 默认只监听 `127.0.0.1:9988`，API 只有 `GET /api/data`、`GET /api/health` 和 `GET /`，没有写接口。页面分别显示下载流水线进度与库存目录扫描进度。库存扫描状态来自 `/var/lib/bmguangya/library.progress.json`，展示扫描阶段、耗时、心跳、已发现/已完成/待处理目录、文件数和脱敏错误；库存没有时 API 仍返回 503，同时页面显示已有扫描进度，不会伪造库存。

最简单的安全访问方式是在本机建立 SSH 本地端口转发：

```bash
ssh -N -L 9988:127.0.0.1:9988 user@server.example.com
```

完成手工转发后，在本机打开 `http://127.0.0.1:9988`。

如果要用仓库提供的可选 `bmguangya-gui-tunnel.service`，先在运行该 unit 的主机上为 root 配置 SSH Host alias。把示例主机名、用户名和私钥路径替换成自己的值；该 unit 默认连接 `bmguangya-host`，并将本机 `127.0.0.1:19988` 转发到远端 monitor 的 `127.0.0.1:9988`：

```sshconfig
Host bmguangya-host
    HostName server.example.com
    User your-user
    IdentityFile /root/.ssh/id_ed25519
    IdentitiesOnly yes
```

保存到 `/root/.ssh/config` 并限制配置及私钥权限后，再安装并启用该 unit。若 SSH alias 使用其他名称，需同步修改 unit 的 `ExecStart` 最后一个参数。也可以跳过这个可选 unit，直接使用上方的手工 SSH 转发。

```bash
sudo install -m 0644 systemd/bmguangya-gui-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bmguangya-gui-tunnel.service
```

启用该 unit 后，在本机打开 `http://127.0.0.1:19988`。若要提供 HTTPS 反向代理，应由同机 Caddy/nginx 或可信私网网关终止 TLS，并加上强认证；不要将转发端口直接暴露到公网。例如手工转发时 Caddy 可代理本机 `9988`，启用该 unit 时应代理 `19988`：

```caddyfile
monitor.example.com {
    basic_auth {
        viewer <bcrypt-hash>
    }
    reverse_proxy 127.0.0.1:19988
}
```

示例域名和认证哈希必须替换为自己的值。不要把 monitor 配置成 `0.0.0.0` 监听。

## 配置值和运行产物

| 路径 | 用途 |
| --- | --- |
| `/etc/bmguangya/config.json` | 运行配置及凭据；建议 `root:root`、权限 `0600`，不提交到 Git。 |
| `/var/lib/bmguangya/library.json` | 最近一次完整 Bangumi/AList 库存；仅成功完整扫描会替换。 |
| `/var/lib/bmguangya/library.progress.json` | 当前或最近一次库存扫描的脱敏进度。 |
| `/var/lib/bmguangya/library.json.failure.json` | 最近一次不完整/失败扫描记录。 |
| `/var/lib/bmguangya/state.json` | 下载器去重、任务、Mikan 映射与运行状态。 |
| `/var/lib/bmguangya/sync-progress.json` | 只读 monitor 显示的下载进度。 |
| `/var/lib/bmguangya/reconcile-report.json` | 云端整理动作、冲突、阻塞项与复核结果。 |
| `/var/lib/bmguangya/strm-sync-report.json` | STRM/NFO 计划、未解析项、归档与 Jellyfin 刷新请求结果。 |
| `/var/lib/bmguangya/archive/` | 旧 STRM 和本程序生成的 Bangumi NFO 的本地归档，不在媒体库路径内。 |

若要在整理前后保留完整云盘目录树报告，可运行 `scripts/export_library_tree.py`。它只读取指定库存快照并输出 Markdown，不加载凭据、不访问网络，也不会把差集伪造成云端文件：

```bash
sudo install -d -m 0750 /var/lib/bmguangya/reports
/opt/bmguangya/.venv/bin/python /opt/bmguangya/scripts/export_library_tree.py \
  --inventory /var/lib/bmguangya/library.json \
  --output /var/lib/bmguangya/reports/cloud-tree-before.md
```

整理和重新扫描后将输出文件名改为 `cloud-tree-after.md`，保留两份快照供比较。

## 测试与 CI

本地运行完整离线测试：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q scripts monitor tests
```

GitHub Actions 在 Ubuntu 上使用 Python 3.11 与 3.12 安装运行依赖并执行上述测试和编译检查。CI 不配置生产 secret，不连接真实业务 API；测试中使用模拟响应和本机临时 HTTP 服务。仓库采用 MIT License，详见 [LICENSE](LICENSE)。
