# Story Video Skill

剧情视频 skill 与可重建运行环境。当前源码已包含本地实际使用的 `produce-story-video`、视频任务工具，以及对白句尾和完整原生音轨的保留逻辑。

源码、锁定的 Python 依赖清单和安装脚本随仓库分发，FFmpeg 原始发行包作为 Release 附件。模型权重由使用者在外部目录准备。密钥、本机配置、虚拟环境、缓存、生成素材和历史制作记录不进入版本库。

## 下载

- 当前版本：克隆本仓库，或使用 GitHub 的 Code → Download ZIP。
- [历史运行依赖 ZIP](./story-video-runtime-v0.1.0.zip)：保留的 v0.1.0 环境包，不含后续 skill 与运行代码更新；新安装使用当前仓库源码。
- [FFmpeg Windows x64](https://github.com/mooncity0126/story-video-skill/releases/download/runtime-v0.1.0/ffmpeg-9.0.2-essentials-windows-x64.zip)：9.0.2 essentials 原始 ZIP，保留发行方许可证和说明。
- [Release](https://github.com/mooncity0126/story-video-skill/releases/tag/runtime-v0.1.0)：版本与附件。

以下命令在当前仓库根目录执行。

## 重建环境

目标环境为 Windows x64、Python 3.12。本地 Prompt-Rewriter 使用单独的 CUDA Python 环境；图像和视频生成使用配置的云端 API。

先查看安装计划：

```powershell
py -3.12 scripts/bootstrap.py --dry-run
```

安装两套独立环境、下载并校验 FFmpeg、生成本机配置：

```powershell
py -3.12 scripts/bootstrap.py --model-root "D:\Models\Prompt-Rewriter"
```

已有 FFmpeg 附件时：

```powershell
py -3.12 scripts/bootstrap.py --ffmpeg-archive "D:\Downloads\ffmpeg-9.0.2-essentials-windows-x64.zip" --model-root "D:\Models\Prompt-Rewriter"
```

也可以使用 `--component harness` 或 `--component rewriter` 分别安装。安装脚本创建虚拟环境、按版本锁安装 Python 包、执行 `pip check` 并生成 `config/local-settings.json`；安装过程不加载模型或提交生成任务。

## 解压后的内容

```text
story-video-skill/
  runtime/video-harness/       API、参数校验、媒体处理和工作流模块
  runtime/prompt-rewriter/     本地 H3 改写入口、官方提示词模板、依赖锁
  scripts/bootstrap.py        环境重建和 FFmpeg SHA256 校验
  config/skill-runtime.example.json
  runtime-manifest.json       工具版本、Release 地址和校验值
  licenses/                   上游许可证与发行说明
  .agents/skills/produce-story-video/  当前剧情视频 skill、规则与原子操作
  scripts/media.py             项目统一入口
  config/api-keys.example.json 空白密钥模板
```

`video-harness` 的主要依赖为 httpx、pydantic、Pillow、jsonschema；本地改写依赖 PyTorch 2.7.1 CUDA 11.8、Transformers 4.57.6、PEFT、Accelerate 和 bitsandbytes。两个环境各自使用 `requirements-lock.txt`，避免 Pillow 等版本约束互相影响。

## 使用 skill

skill 已位于 `.agents/skills/produce-story-video/`。安装脚本生成 `config/local-settings.json`，绑定本仓库内的两个解释器、video-harness 源码、Rewriter 脚本、FFmpeg 和外部模型目录。默认本地改写、视频并发 4、图片并发 2；每候选一轮视频检查、默认 6 帧，每镜共享一次内容返工。

安装后直接在 `config/api-keys.local.json` 填入 `X2V_API_KEY` 和可选的 `QWEN_API_KEY`，无需设置系统环境变量。该文件受 `.gitignore` 排除；重建环境不会覆盖已有密钥文件。

```powershell
& .\runtime\video-harness\.venv\Scripts\python.exe scripts/media.py doctor
& .\runtime\video-harness\.venv\Scripts\python.exe scripts/media.py init --output runs/my-story
```

从项目调用 `$produce-story-video`，让 AI 根据创意/剧本推进制作。API、模型、预算和路径均在配置里修改。若本机已经安装另一份 skill，可在被忽略的 `config/skill-location.json` 中指定 `skill_root`，或设置当前进程的 `STORY_VIDEO_SKILL_DIR`；默认使用仓库内的版本。

写作先确定人物目标、阻力、行动后果与结局，再完成可读的场景剧本。分镜先安排整场戏的动作、对白和必要声音，再按信息变化确定切镜及合法时长；不再机械地按一句台词一个镜头拆分。方法改编自 `zenstory-ai/drama-skills`，并入现有创作和一次内容/时长检查，无新增依赖、付费调用或检查轮次。

对白按自然语速规划，最后一句后预留 0.8–1 秒。视频模型同时生成对白与音效；合成保留完整原生音轨，声音长于画面时延续最后一帧。请求时长之和与最终实长分开记录，用户片长上限不用于硬切声音。

外部模型目录结构：

```text
Prompt-Rewriter/
  Qwen3-VL-8B-Instruct/
  MiniMax-H3-Prompt-Rewriter-LoRA-8B/
```

来源分别为 Qwen/Qwen3-VL-8B-Instruct 和 lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA-8B。本版本分发运行代码与安装清单，模型权重、历史生成结果和本机虚拟环境留在各自机器上。

## 验证与来源

发布前执行源码语法检查、离线测试和安装计划检查。已有本地视频制作验证；本次源码整理仅执行离线测试，不新增付费生成。

来源和打包调整见 `THIRD_PARTY_NOTICES.md`，当前测试结果见 `VALIDATION.md`。FFmpeg 发行文件按 SHA256 校验；历史 ZIP 和对应校验文件保持原版本。
