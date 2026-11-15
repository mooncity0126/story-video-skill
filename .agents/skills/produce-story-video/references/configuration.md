# 配置字段与支持能力

`configs/default.json` 为起点。创建项目后使用保存了绝对路径的 `settings.json` 副本。配置保存模型、接口和凭据名称；实际密钥可由 `credentials_file` 指定的本地 JSON 文件提供。

在项目的 `config/api-keys.local.json` 中填写 `X2V_API_KEY` 与 `QWEN_API_KEY` 即可，无需设置 Windows 环境变量；首次安装由 `config/api-keys.example.json` 创建空白副本。文件只允许包含 provider 的 `key_env` 所指定的名称，值为字符串。文件内的值优先，显式空值表示未配置；没有在文件中列出的名称仍可使用进程继承的环境变量。未设置 `credentials_file` 的旧配置继续使用原有环境变量方式。

`credentials_file` 相对配置文件所在目录解析；新建制作目录时只保存密钥文件的绝对路径，不复制密钥内容。每条命令重新读取密钥文件，检查仅报告是否存在。适配器所需的环境值只在当前命令内临时提供，结束时恢复，不修改 Windows 用户或系统环境变量。

## 配置分组

| 分组 | 内容与执行者 |
|---|---|
| `credentials_file` | 可选的本地密钥 JSON 文件路径；路径进入制作记录，密钥值不进入记录 |
| `runtime` | `video-harness` 源码与 Python 路径；脚本导入对应模块，调用方使用配置的 Python |
| `providers` | 媒体网关、文本/视觉 endpoint、模型、凭据变量、超时、输出长度；由 API 适配器读取 |
| `models.image/edit/video` | 各角色的 provider、task、model_cls、stage、parameters、media_field；由请求脚本读取 |
| `rewriter` | 本地适配是否启用、兼容视频模型、解释器/脚本/权重目录和资源参数；由 `rewrite` 使用 |
| `production` | 画幅、风格、总时长及容差；AI 选择目标尾帧、字幕，脚本执行媒体并发与导出约束 |
| `review` | 检查选择和次数由 AI 执行；抽帧参数和视频返工上限由脚本执行 |
| `execution` | 媒体积分、请求重试、超时、文件大小、外部文本/视觉调用与已报告 token 限额 |
| `media` | FFmpeg/ffprobe、导出帧率、尺寸和缩放方式 |

紧凑节奏与台词/音效计划由 AI 按 [workflow.md](workflow.md) 的节点 3–4 执行，使用已有配置与原子命令，不增加检查开关。`production.audio: native` 表示画面、对白、环境声和动作音效交给视频模型一起生成；不自动接入额外声音服务。人物的声音描述和每镜对白/音效写入创作文件与源提示词。

`production.target_duration_ms` 是已选分镜的请求时长之和，用于核对时间线；它不是成片裁切点。`production.max_duration_ms` 为用户明确的成片上限，默认 `null`。例如“2 分钟以内”写为 120000，“5 分钟以内”写为 300000，并在分镜中保留原始约束。按已有素材的实际超出量预留整片空间，完整音画超过上限时调整分镜，不硬切音轨。

普通镜头优先按 5–6 秒估算并与实时模型规格取交集；对白须容纳自然语速、停顿和说完后的 0.8–1 秒余量，放不下就延长或精简。时段与余量写入分镜和 Rewriter 源提示词，不在 `models.video.parameters` 固定统一时长。采样、实际尾帧和合成保留完整源素材，声音长于画面时延续最后一帧；仍使用模型原生声音，不新增声音检查配置。

文件路径相对配置所在目录解析；`%USERPROFILE%` 等环境路径会展开。初始化后保存绝对路径。`runtime.python` 指定调用时使用的解释器。

## 并发与检查默认值

| 字段 | 默认值 | 含义 |
|---|---|---|
| `production.maximum_parallel_video_jobs` | `4` | 在途视频上限 |
| `production.maximum_parallel_image_jobs` | `2` | 在途 `image` 与 `edit` 合计上限 |
| `production.maximum_parallel_media_jobs` | `6` | 在途媒体总上限，和分类上限共同生效 |
| 本地 Rewriter 并发 | `1` | 由现有适配器单任务锁执行 |
| `review.content_check_rounds` | `1` | 每版分镜一次内容/时长检查 |
| `review.frame_check` / `prompt_check` | `on_demand` | AI 按镜头选择；也支持 `always` 或 `off` |
| `review.frame_check_rounds` / `prompt_check_rounds` | `1` | 启用的检查每个候选一次 |
| `review.video_check_rounds` | `1` | 每个视频候选一次 |
| `review.sampling` / `include_endpoints` | `uniform_count` / `true` | 按保留片段帧序号均匀选样，含首末帧 |
| `review.sample_frames` / `sample_max_edge` | `6` / `512` | 每候选采样总量与图片最长边；采样数可配置 2–12 |
| `review.max_content_repairs_per_shot` | `1` | 每镜共享的内容返工次数，支持 0 或 1 |
| `review.mode` | `agent` | 当前 AI 执行视觉判断；`vision_api` 改用配置的视觉 API 完成同一轮 |

在途状态包括 `submitting`、`submission_unknown`、`submitted`、`waiting_remote`。成功或明确失败后释放并发名额。`status` 返回当前占用、上限和可用名额；`submit` 在任务锁内执行分类与总量限制。旧配置缺少分类上限时沿用其总上限。

检查轮数目前均为 1。可选节点通过 `on_demand/always/off` 选择；AI 将每镜的选择与触发理由写入分镜。跨节点返工次数写入 AI 维护的 `repair-ledger.json`；脚本执行视频候选的返工上限。增加轮数需要同步更新工作流与执行逻辑。

## 模型与协议

- 媒体支持 `lightx2v_cloud` 的模型规格、报价、提交、查询和下载协议。三个角色可引用不同兼容 provider；每个任务记录实际服务，查询回到对应服务。
- 文本/视觉支持 `chat_completions`，按单次工作返回 JSON。视觉模型需支持图片输入。当前 AI 可直接完成创作与检查，也可使用配置的外部模型完成对应节点。
- 本地改写采用 `LocalPromptRewriter` 的 H3 适配。权重路径与资源参数可配置；首帧或首尾帧输入由实际素材决定。新增模型架构或云端 Rewriter 通过新增适配器接入。
- 字幕仅在用户要求时通过可用的转写/对齐工具制作；当前原子脚本提供音轨提取与成片合成。对白/音效直接使用原生音轨，不增加独立音频服务或额外声音检查。

选用模型时读取本项目缓存的实时规格，核对时长、参考图数量、首尾帧支持和参数。模型配置改变后重新取得对应规格。当前媒体处理的单镜上限为 15 秒。

`content_policy: warn` 保存改写原文并返回语义提示；结构、实际图像、时长和画幅的校验由适配器执行。本地改写启用时，视频参数使用 `enhance_prompt: false`，视频提交使用已保留的改写正文。

## 项目与费用

`budget_credits: null` 表示本次媒体积分预算待确定；实际生成前把用户已授权预算写入项目副本。文本/视觉用量与本地推理耗时分别记录。`max_text_tokens` 按已返回的 usage 累计；服务侧消费限额可与其配合使用。

每个制作项目有独立的配置、素材和任务目录。恢复操作依据本项目状态；未知提交通过 `resolve` 记录核实结果后继续。依赖模块升级后运行 skill 的离线测试。
