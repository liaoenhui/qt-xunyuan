# QT 视频数据生产工具

面向单人、短周期生产的本地 MVP。设计目标是提高单位时间合格视频产出量，同时严格遵守《QT寻源数据规则（供应商用）》：程序只对可确定的硬失败自动淘汰，低置信度视觉判断保持 `UNKNOWN`，PDF 内部冲突保持 `CONFLICT`，最终语义判断由人工完成。

## 已实现

- 单一业务规则源：`rules/qt_rules_v4.yaml`
- 显式冲突库：`rules/conflicts.yaml`
- 47 个详细子单元和搜索模板
- `PASS / FAIL / UNKNOWN / NOT_APPLICABLE / CONFLICT` 五态规则引擎
- R1-R17、规格检查、T6 环视/回访、R10/R13 豁免和边界冲突
- YouTube 扁平元数据寻源、URL 导入、`platform + video_id` 去重
- 自动检索默认过滤超过 10 分钟的来源，代理下载接口也执行同一资源上限
- 元数据 / 480p 代理 / 最终源三级下载
- 最终源优先原站最高至 4K 的音视频格式，不做放大或插帧；无该范围格式才回退更高分辨率。8K 等大素材剪片限制解码与编码线程，避免自动线程分配占用过量内存。
- FFmpeg 镜头检测（默认场景阈值 0.28）、安全边距、少于 5 秒镜头过滤
- 人物主体连续性检测（13 个以人为主体的单元）：先过镜头切换，再把持续离场区间切开，不整条淘汰；无 5 秒以上子段时标记「保留整段待人工」
- 动作边界辅助：仅在主体持续离场且出现明确、连续的运动上升前转折点时给出建议，最多回退 3.5 秒；建议不会自动生效，人工审核页可采用建议、当前位置设点和保存裁剪
- 代表帧 dHash 二次去重
- ffprobe、静默、黑帧、宽高比、分辨率、帧率、编码和音轨检测
- 实拍规格预检与最终 QA 统一认可标准 24p：24000/1001（约 23.976fps）按 24p 接受，保留真实帧率、不插帧；不将其他低于 24fps 的帧率整体放宽。
- 人工审核页按 T1–T9/未分类筛选并每页加载 20 条，支持键盘快捷键、规则原文/页码抽屉、标签覆盖和硬失败二次确认
- 接受后高质量源下载、精确剪片、独立最终 QA、两级交付目录
- SQLite 持久化、操作追溯、流量统计、配额仪表盘、CSV 交付表
- 生产台各列表独立分页，每页 20 条；候选与已分析来源可按 T1–T9/未分类筛选
- 代理与最终源物理隔离；交付代码拒绝从 `proxy` 目录复制

规则文件使用 JSON 语法（JSON 是 YAML 1.2 的合法子集），因此核心运行时不依赖 PyYAML。

## 当前寻源题材库（2026-09-22）

客户补充：**目前不采集第一人称**。新版 [活动题材库](web/topic-library.html) 覆盖全部 53 个详细单元，按外部拍摄、明确主体、完整动作重新选题；点击 T1.1 等分类即可复制关键词、打开搜索或填入生产台，不会自动搜索或下载。

- 正向关键词移除 POV、FPV、驾驶舱、头胸佩戴等第一人称方向；T3/T4 改用双人互动、职业操作、厨艺手工等具体活动。
- 搜索词不保证返回结果的人称或合格率，仍先看缩略图／短预览，再检查规格和下载代理。仅手部顶拍、自拍、无人空镜不直接判为第三人称。
- 原始规则和历史数量不改写；页面将旧规划折叠保留，不把第一人称配额自动转为第三人称。审核页与仪表盘已不再区分人称，配额按桶内已接受总数统计。
- `docs/` 中 9 月 20、21 日的旧关键词文档保留归档，其中第一人称方向已停用，以新版 HTML 为准。

维护入口是 `docs/activity_topics.txt`（活动、短词、选片提醒）。修改后运行 `python scripts/build_topic_library.py`，同时更新 `web/topic-library.html` 与 `rules/search_templates.yaml`，防止两套词库不一致。生成器和测试会检查正向关键词是否混入第一人称词。

## 运镜预筛（试运行，只提示）

- 镜头切分后，复用低清代理逐片段估计背景整体运动；不把主体动作、轻微抖动或画面缩放当作有效运镜。
- 人工审核新增“运镜预筛”面板：疑似固定／抖动、疑似变焦、持续整体运动、混合、无法判断、尚未检测。可与 T1–T9 分类组合筛选，仍每页 20 条；展开时间段可点击定位。
- 历史待审核候选可点“补做运镜检测”，不重新切镜、不重新下载。保存人工裁剪后会清除旧运镜结论，需要重新检测。
- 该功能**不会自动拒绝、自动通过或自动裁剪**，不改历史交付和原始规则。变焦不合格，但自动缩放提示不能可靠区分镜头变焦、数字推近和真实推拉，必须人工核验，R15 仍由人工确认。
- 基于 OpenCV 背景特征／光流／鲁棒变换，最多使用 480 像素宽图像、约 4fps 抽样、3 秒窗口；单次检测有 20 秒软预算，缺依赖、超时或背景证据不足均返回“无法判断”。不需要下载模型。
- 实拍快速跟拍的视差、近景遮挡、背景低纹理可能产生较多“无法判断”；动画／屏幕内容的整体位移不代表真实运镜。现阶段不宣称真实素材准确率，需收集人工对照样本后再决定是否启用自动淘汰。

回归测试：`python -m unittest tests.test_camera -v`，包含固定背景局部动作、平移、旋转、抖动、变焦、混合、低纹理、超时，以及状态不变、裁剪失效、筛选分页等安全检查。

## 快速启动

在 PowerShell 中运行：

```powershell
./start.ps1
```

首次启用主体连续性切分时运行一次：

```powershell
./install-subject-ai.ps1
```

该脚本安装 OpenCV，并把约 22 MB 的离线人物检测模型放入 `tools/models/`；视频帧不会上传到外部服务。

然后打开：

```text
http://127.0.0.1:8765
```

本地开发工作区可在 `tools/` 内放置 FFmpeg、ffprobe 和 yt-dlp，程序会自动发现；该目录包含大型运行时文件，不提交到 Git。克隆仓库后请自行安装这些命令行工具，或复制 `.env.example` 为 `.env` 并配置其路径：

```text
FFMPEG_BIN=C:\path\to\ffmpeg.exe
FFPROBE_BIN=C:\path\to\ffprobe.exe
YTDLP_BIN=C:\path\to\yt-dlp.exe
YTDLP_JS_RUNTIME=node:C:\path\to\node.exe
YTDLP_COOKIES_FROM_BROWSER=firefox
YTDLP_COOKIES_FILE=C:\path\to\www.youtube.com_cookies.txt
YTDLP_PO_TOKEN_URL=http://127.0.0.1:4416
YTDLP_PROXY=http://127.0.0.1:7890
SOURCE_MAX_DURATION_SECONDS=600
YTDLP_SLEEP_INTERVAL=5
YTDLP_MAX_SLEEP_INTERVAL=10
```

YouTube 的公开素材解析需要 JavaScript 运行时。程序会自动探测 Deno 或 Node.js，也可通过
`YTDLP_JS_RUNTIME` 手工指定。不要在配置文件中填写 YouTube 账号、密码或验证码。
如果公开取流触发登录验证，可通过 `YTDLP_COOKIES_FROM_BROWSER` 使用本机已关闭浏览器的
登录会话；该配置只保存浏览器名称，不保存 Cookie 内容。建议使用专用 Firefox 资料和备用账号。

浏览器会话不可用时，可改用 Cookie 文件：用 `yt-dlp` 支持的 Netscape 格式从浏览器导出，
通过 `YTDLP_COOKIES_FILE` 指定路径，留空时程序自动探测项目根目录下的
`www.youtube.com_cookies.txt`。Cookie 文件等同于登录凭据，切勿提交到 Git，
仓库 `.gitignore` 已忽略 `*cookies*.txt`；两者同时配置时优先使用 Cookie 文件。

YouTube 的 bot 检测还可能要求 PO Token。安装 yt-dlp 插件 bgutil-ytdlp-pot-provider
并在本机启动其 HTTP 服务后，把服务地址填入 `YTDLP_PO_TOKEN_URL`（留空则不启用），
首页工具状态栏的 `po_token` 会显示该服务是否可用。需要经代理访问时可配置 `YTDLP_PROXY`。

## 生产流程

1. 在“生产台”输入英文搜索词和目标单元，只拉取元数据；默认过滤超过 600 秒的来源，可通过 `SOURCE_MAX_DURATION_SECONDS` 调整。标题命中 `rules/search_templates.yaml` 的 `defaults.negative` 或内置避开词（POV、GoPro、cinematic、vlog 等）的结果不入库。
2. 根据分数和配额缺口选择少量来源，点击“下载代理”。
3. 点击“镜头分析”，系统先检测镜头切换，再对以人为主体单元的单镜头段检测人物主体连续性；主体持续离场时会在最后安全画面处结束当前段，并把重新出现后的不少于 5 秒片段另建候选，而不是淘汰整条来源；若切开后没有任何不少于 5 秒的子段，则保留整段并标注待人工处理。
   分析成功后来源自动进入“已分析来源”，需要重做时可移回候选来源。
4. 进入“人工审核”，可先按 T1–T9 分类筛选，再按 T1.1 等单元细筛，每页 20 条；再查看动作边界风险，可采用系统建议，或播放到准确位置后用“当前位置设起点/终点”微调并保存，再用 `A / R / E / Space / ← / →` 审核。
5. 接受时必须确认桶、单元；确定性硬失败需要二次确认并记录覆盖。
6. 回到生产台，对已接受候选点击“最终处理”。
7. 最终 QA 为 `PASS` 时，文件进入 `data/deliverable/CS/T{桶}/`，与正式 OSS 路径
   `oss://futurelab-game-hz/game_data/QT寻源全包供应商正式作业/CS/T{桶}/` 一一对应；
   文件名固定为 `T{桶}.{单元}_{序号3位}_{简述}.mp4`（例：`T7.6_001_液体界面移动.mp4`），
   序号在同一单元内递增，简述取审核页填写的“交付文件简述”，留空时回退到来源标题。
8. 点击“导出交付表”生成 UTF-8 BOM CSV；前 9 列与共享人效表 Sheet1 对齐：
   “时间 / 领取人 / oss链接 / 视频时长 / 桶 / 桶的具体类目 / 内部质检 / 验收 / 备注”，
   可直接粘贴进台账；其后附加“单元 / 分辨率 / 时长秒 / 本地路径”便于核对，粘贴时去掉即可。
   “视频时长”填档位字符串 `5-15S` / `15-30S` / `30-60S`；“领取人”取 `DELIVERY_OWNER`，
   OSS bucket、endpoint、prefix 由 `OSS_BUCKET` / `OSS_ENDPOINT` / `OSS_PREFIX` 配置，
   工具只生成路径，不负责上传，也不保存任何 AccessKey。
   最终处理通过的候选在导出后进入“已处理”，也可以移回最终处理列表重新操作。

代理资源只用于切镜与人工预览。R9 分辨率硬判定会延后到最终原视频下载、精确剪片之后执行，
不会再用 480p 代理分辨率淘汰候选。同一来源产生的最终分片按时间顺序命名为
`来源文件名-1.mp4`、`来源文件名-2.mp4`……。

YouTube 若对某条视频要求登录或 Cookie，工具会直接报错，不会绕过登录、验证码、DRM 或平台限制。可换用公开可访问来源，或只登记候选后人工处理合法素材。

## 命令行批量找源

寻源到镜头分析这三步可以脱离网页，用 `tools_batch.py` 无人值守跑，把人工时间集中在审核页。
在仓库根目录运行：

```powershell
python tools_batch.py discover T7.4 --queries 6 --limit 10   # 取该单元前 6 条搜索词，每条最多 10 个结果
python tools_batch.py proxy    T7.4 --top 20                 # 未下代理的来源按 source_score 降序取前 20 条
python tools_batch.py analyze  T7.4                          # 对已有代理且未分析的来源做镜头分析
python tools_batch.py run      T7.4                          # discover → proxy → analyze 连跑
python tools_batch.py status   T7.4                          # 来源/候选状态与自动淘汰原因 Top 10
python tools_batch.py tools                                  # 打印 ffmpeg / ffprobe / yt-dlp 等可用性
```

- 单元参数可以写完整子单元（`T7.4`），也可以写整桶（`T7`，按子单元顺序依次取搜索词）。
- 搜索词来自 `rules/search_templates.yaml`，与网页生产台使用同一套规则和分辨率门槛。
- `proxy` 会先做规格预检：`FAIL` 计入 `skipped_preflight_fail` 并跳过；`UNKNOWN` 默认跳过并计入
  `skipped_preflight_unknown`，确认要下载时加 `--allow-unknown`；其余失败计入 `failed` 并打印原因。
- 每行输出都带 `[HH:MM:SS]` 时间戳。Windows 控制台如出现中文乱码，请在外部设置 `PYTHONIOENCODING=utf-8`。
- 命令行不创建网页的后台任务记录，因此生产台的“任务进度/取消”按钮看不到它，来源状态与候选结果一致可见。

网页与命令行共用同一个 SQLite（`data/qt_tool.sqlite3`，`journal_mode=WAL`、连接 `timeout=30`），
可以同时开着：一边用命令行批量找源、下代理、跑分析，一边在
`http://127.0.0.1:8765/review` 审核已经产出的候选。为避免重复下载同一条来源，
建议命令行和网页不要同时对同一个单元执行代理下载。

## 数据目录

```text
data/
  metadata/
  proxy/          # 仅分析与审核，禁止交付
  original/       # 人工接受后才下载
  clips/          # 精确剪片输出
  rejected/
  deliverable/    # 唯一交付读取目录
    CS/T7/T7.6_001_液体界面移动.mp4   # 与 OSS 上的 CS/T{桶}/ 同构
    QT寻源数据_交付信息表.csv
  qt_tool.sqlite3
```

## 测试

```powershell
python -m unittest discover -v
```

测试覆盖任务书列出的关键规则样例，包括 4.9 秒失败、三档时长、15/30 秒冲突、素材类型、游戏录制、横竖屏、T6 豁免、T8.4 慢动作豁免和 T8.2 航拍冲突。

## 当前边界

最终源下载采用按来源互斥：同源片段共用一次下载，其他片段等待后复用。不同来源的最终下载并发数由 `MAX_DOWNLOAD_CONCURRENCY` 控制。每次新下载使用独立目录 `data/original/source_<来源ID>/<尝试ID>/`，失败日志为其中的 `download.log`（下载网址已脱敏）。

高清源发布前会核对容器、视频轨和音频轨时长，并完整解码检查；仅文件存在不再视为下载成功。同一服务进程内，未改变的已验证文件不重复解码。已损坏的旧文件移入 `data/original/quarantine/`，不删除，审核记录不受影响；失败尝试保留，不会作为后续剪片源。

AV1 原片优先尝试 NVIDIA 显卡全片解码校验；显卡不可用或检查报错时回退 CPU 完整校验，不跳过检查。独立下载目录的片段缓存带来源 ID，避免不同视频互相覆盖。

- T1.1 T1.2 T1.4 T3.2 T3.3 T3.4 T3.5 T5.3 T6.6 T6.7 T6.8 T6.9 T8.4 已启用离线“显著人物主体”连续性预检；人物身份和语义仍由人工复核。其他单元的动物、载具、人群、手部或场所主体暂不套用人物模型，以免误切；开头检不到显著人物时只标记 LOW_CONFIDENCE，不做切分。
- YouTube 可进行不登录的扁平元数据搜索；视频下载是否可用由来源当时的公开访问策略决定。
- 自动缓存清理暂未启用，避免误删；可按 `data/proxy` 与 `data/original` 状态手工清理。
- OSS 上传不在 MVP 内；交付目录和 CSV 已准备好。

## 规则审计入口

### 操作者大小与完整度（辅助预筛）

客户补充：操作者过小、只露手导致人物不完整，不符合采集要求。新镜头分析会为人物活动单元（T1.1、T3、T4、T7.1/2/3/8）附加构图提示；历史待审核片段可在审核页展开“操作者大小与完整度”补检。动物、载具、纯物理现象不套用人物门槛。

- 结果包括疑似过小、仅见手部、部分身体、混合风险、可见头肩及部分身体、无法判断。可点击抽样区间定位播放。至少连续三次抽样才形成片段风险提示。
- 只给提示，不自动拒绝、接受或裁剪；未检出人物不等于只露手。多人、遮挡、异常姿态、疑似切换到旁观者时应人工确认。可见头肩不等于整条合格，也不要求所有活动都必须拍到脚。
- 大小采用可见关键点包围范围估计，不是真实人体分割面积。试运行提示阈值为面积小于 3.5% 且高度小于 30%（`qt_tool/operator_framing.py` 中常量）；不是客户正式数值标准，须用客户样本校准。
- 每秒约一帧，最长片段最多 60 帧；单进程超时 40 秒、并发 1。组件缺失、繁忙或证据不足返回“无法判断”，不阻止正常审核。低清代理、手部遮挡、离画面边缘很近的手均可能漏检。保存裁剪后旧提示失效，需要重新检测。
- 审核侧栏的接受、拒绝、前后候选与分页独立于详情滚动；运镜、人物检测、规则详情可折叠，风险摘要仍显示。

可选检测环境与主应用隔离安装（Windows，目录已被 Git 忽略）：

```powershell
python -m venv --system-site-packages tools/operator_env
tools/operator_env/Scripts/python.exe -m pip install -r requirements-operator.txt
New-Item -ItemType Directory -Force tools/models
Invoke-WebRequest 'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task' -OutFile tools/models/pose_landmarker_lite.task
Invoke-WebRequest 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task' -OutFile tools/models/hand_landmarker.task
```

安装完成后重启服务即可使用。实际速度受 CPU、片段长度及模型冷启动影响；本功能不替代客户验收。

- `rules/qt_rules_v4.yaml`：唯一业务规则源
- `rules/conflicts.yaml`：PDF 内部冲突与临时策略
- `rules/search_templates.yaml`：只影响候选搜索，不影响业务判定
- 审核页点击任一规则，可查看 PDF 页码、原文、系统解析和当前检测依据
