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
| 几何观察与因果动作规划作为额外阶段 | 独立检查/规划调用 0 次；可选帧检查产出的观察事实供改写使用 |
| 改写后 Prompt 评审作为质量节点 | 普通镜头 0 次，AI 按需启用后每份候选 1 次 |
| 视频按每秒 4 帧采样，最多 24 帧一批，相邻批重叠 1 帧；独立物理检查后另有全片概览检查 | 每候选总计 6 帧，包含首末帧，合并为 1 次内容检查 |
| 可配置每镜人工确认 | 一般检查结果由 AI 按既定结论处理；新增重要方案变更由用户决定 |
| 各检查节点可产生修复建议 | 每镜各内容节点共享 1 次返工，替换候选在对应节点检查 1 次 |

用户确认的其他设置：AI 主导制作，视频并发 4、图片并发 2、本地改写并发 1；目标尾帧按需；使用原生声音，字幕按需；模型/API/路径/限额集中配置。速度比较采用后续实际制作的墙钟时间。

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
