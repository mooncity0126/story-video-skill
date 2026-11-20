# 工作流来源

整理日期：2026-11-06。来源为项目对话、Python 实现和用户提供的流程框架图。

## 参考对话与代码

- 本地项目迭代：本地改写、依赖并行、动态时长、首尾帧及检查流程。
- 制作反馈：保留失败后的任务记录，以完整动作组织分镜；对白预留句尾时间，合成完整保留原生音轨。

相对 `runtime.harness_root` 的模块：

| 文件 | 采用的能力 |
|---|---|
| `src/video_harness/client.py` | API、提交状态、查询、结果下载 |
| `src/video_harness/contracts.py` | 模型参数、图像输入规格与请求校验 |
| `src/video_harness/rewriter.py` | 实际首帧/首尾帧改写、单任务锁、缓存与原文 |
| `src/video_harness/media.py` | 媒体读取、实际尾帧与时间线合成 |
| `src/video_harness/text.py`、`quality.py` | 可选的单次文本/视觉调用 |
| `src/video_harness/engine.py`、`planning.py` | 节点与依赖的设计参考；本 skill 的跨节点调度由 AI 执行 |

## 检查节点的对应变化

下表的原设置指 `models.load_config()` 创建项目时启用质量检查的分支及其 `engine.py` 实现；历史运行使用各自保存的配置。

| 原实现 | 本 skill 默认设置 |
|---|---|
| 独立首帧检查；启用目标尾帧时增加尾帧检查，特定配置再检查固定机位 | 首尾帧合并为一组，普通镜头 0 次，AI 按需启用后每组 1 次 |
| 几何观察与因果动作规划作为额外阶段 | 独立检查/规划调用 0 次；可选帧检查产出的观察事实供编写最终提示词使用 |
| 改写后 Prompt 评审作为质量节点 | 普通镜头 0 次，AI 按需启用后每份候选 1 次 |
| 视频按每秒 4 帧采样，最多 24 帧一批，相邻批重叠 1 帧；独立物理检查后另有全片概览检查 | 每候选总计 6 帧，包含首末帧，合并为 1 次内容检查 |
| 可配置每镜人工确认 | 一般检查结果由 AI 按既定结论处理；新增重要方案变更由用户决定 |
| 各检查节点可产生修复建议 | 每镜各内容节点共享 1 次返工，替换候选在对应节点检查 1 次 |

用户确认的其他设置：AI 主导制作，视频并发 4、图片并发 2；目标尾帧按需；使用原生声音，字幕按需；模型/API/路径/限额集中配置。本地改写原为并发 1，2026-11-18 按用户要求停用；其锁与环境保留。速度比较采用后续实际制作的墙钟时间。

## 2026-11-15：整合剧本与整场分镜方法

参考仓库为 [zenstory-ai/drama-skills](https://github.com/zenstory-ai/drama-skills)，`zenstory-ai` 是仓库所有者，`drama-skills` 是仓库名。参考版本固定为 [`8cf7404981b7095b498e061832bb3c9ffd6a42be`](https://github.com/zenstory-ai/drama-skills/tree/8cf7404981b7095b498e061832bb3c9ffd6a42be)，不是运行依赖，不在制作中下载或调用上游服务。

本地 [story-writing.md](story-writing.md) 和 [scene-planning.md](scene-planning.md) 依据以下文档的方法重新组织，并适配本项目的短片时长、原生音画、独立片段生成与既有检查次数：

| 上游文件 | 纳入的内容 |
|---|---|
| [script-craft.md](https://github.com/zenstory-ai/drama-skills/blob/8cf7404981b7095b498e061832bb3c9ffd6a42be/skills/short-drama-write/references/script-craft.md) | 人物具体目标、阻力与行动后果、场景变化、选择与代价、可拍动作、情绪落点 |
| [dialogue-craft.md](https://github.com/zenstory-ai/drama-skills/blob/8cf7404981b7095b498e061832bb3c9ffd6a42be/skills/short-drama-write/references/dialogue-craft.md) | 对白目的、回应关系、人物表达差异、有依据的潜台词、动作与说话的配合 |
| [scene-sound-dramaturgy.md](https://github.com/zenstory-ai/drama-skills/blob/8cf7404981b7095b498e061832bb3c9ffd6a42be/skills/short-drama-write/references/scene-sound-dramaturgy.md) | 对剧情有作用的声音事实、静默与触发，不把配乐作为情绪替代 |
| [shot-craft.md](https://github.com/zenstory-ai/drama-skills/blob/8cf7404981b7095b498e061832bb3c9ffd6a42be/skills/short-drama-storyboard/references/shot-craft.md) | 整场事件先于切镜、镜头作用、动作只在主要位置完成、对白和动作共同估时 |
| [blocking-playbooks.md](https://github.com/zenstory-ai/drama-skills/blob/8cf7404981b7095b498e061832bb3c9ffd6a42be/skills/short-drama-storyboard/references/blocking-playbooks.md) | 固定空间锚点、人物/道具位置、视线及机位变化时的方向连续性 |

上游采用 MIT 许可证，版权为 `Copyright (c) 2026 drama-skills contributors`；完整文本随 skill 保存在 [licenses/drama-skills-MIT.txt](../licenses/drama-skills-MIT.txt)。本文档中的排练室时间示例为本项目新增示例。

本次只补充原有创作及一次内容/时长检查的判断依据。未引入上游的制作服务、独立配音、跨片段连续语音、多镜容器、声音剪辑或额外评审/确认流程；不改变对白句尾余量、完整音轨合成、候选复用、并发及返工限额。没有重新生成历史成片。

## 2026-11-18：重点整合视频提示词、直接编写 H3 正文

本次按用户要求采用更新后的 [short-drama-video-prompts](https://github.com/zenstory-ai/drama-skills/blob/37232f6d22e174c36e321d2e3a1a3ae871e7d602/skills/short-drama-video-prompts/SKILL.md)，固定版本为 `37232f6d22e174c36e321d2e3a1a3ae871e7d602`。方法改编保存在 [video-prompts.md](video-prompts.md)、[minimax-h3.md](minimax-h3.md) 及更新后的整场规划中；沿用随包附带的 drama-skills MIT 版权声明。

| 上游资料（同一固定版本） | 本地采用内容 |
|---|---|
| `short-drama-video-prompts/SKILL.md` | 输入/模式、时间容纳、动态过程三步；提示词不擅改定稿台词、起止状态或镜长；可直接复制的正文 |
| `references/video-prompt-guide.md` | 减少静态复述，描述动作过程、物品变化、声音触发与结束状态 |
| `references/performance-action-timing.md` | 情绪刺激和回应、景别可见的表演信号、口语语气变化、物理动作次序和并行估时 |
| `references/camera-audio-continuity.md` | 运镜与分镜一致、画内/画外/旁白区分、声画触发。涉及前镜状态时依照本项目的实际尾帧依赖执行。 |
| `references/model-and-delivery-profiles.md` | 当前模型能力与原生音画 `same_pass` 路线、独立生成与内部切镜的组织边界 |
| `references/minimax-h3.md` | H3 三字段/六字段区别、逐字中文对白、固定说话人、表演时长和参考素材限制 |

上述 `references` 路径均相对上游 `skills/short-drama-video-prompts/`。本地依赖映射见 [video-prompts.md](video-prompts.md) 的输入表：剧本、资产设定、分镜和项目配置由本 skill 已有文件提供，投产/合成由现有原子脚本执行。因此没有新增必须安装的上游技能或生产服务。

H3 实际输出格式核对 [MiniMax-AI/MiniMax-H3 官方指南](https://github.com/MiniMax-AI/MiniMax-H3/blob/199b7d0e1fd95e7b3eafa8c15eca692534142427/skills/h3-prompt-writing/references/base-en.txt)，固定版本 `199b7d0e1fd95e7b3eafa8c15eca692534142427`：首帧/首尾帧对齐首行、三个核心字段、英文动作与中文原文、稳定说话人、官方切镜和声音跨内部切点语法。只保留相关模板与本项目新示例，不复制整份上游手册。

用户指定 H3 约 4.1 个中文字/秒作为规划基准；本地按实读长度计算，再考虑必要停顿、动作重叠和最后一句后 0.8–1 秒余量。当前网关规格为 5–15 秒整数，优先合并短反应，必要时在同一个请求内用 H3 官方语法安排少量有作用的切镜；这不增加新的多镜容器服务、请求或检查轮次。

默认本地 `rewriter.enabled: false`，服务端 `enhance_prompt: false`。旧环境、权重及记录保留；直写模式不依赖它们。未接入上游的参考音频、Ref2VA、独立配音、声音跨独立请求混剪、ASR 对词或额外检查。既有每候选 1 轮/6 帧、每镜 1 次内容返工、完整原生音轨和预算规则保持有效。更新验证使用离线模拟，不重新生成历史视频。

## 2026-11-18 工作流整理

按本地《Video-Harness-Workflow-Memory》《Video-Harness-Skill-Memory》、11 月 10 日安装包和整合前后的 Git 备份核对节点。原有串行链覆盖连续动作、人物站位与关键物品状态，也覆盖切换机位、插入空镜后重新出现的同一状态。独立镜头或只共享稳定外观的镜头可按依赖并行。已把这些条件落实在素材准备、素材确认和视频提交中。

后续整合外部写作方法时，在原有节点中记录新增方法、输入与输出。涉及变更制作顺序、参考素材、模型、检查次数或交付方式的提案，列明改变的行为及理由，由用户另行决定。
