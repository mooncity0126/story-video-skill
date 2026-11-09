# Story Video Skill

剧情视频 skill 的运行依赖发布仓库。当前版本先交付运行环境，后续 skill 放入 `.agents/skills/`。

本次采用可重建环境：源码、锁定的 Python 依赖清单和安装脚本随运行包分发，FFmpeg 原始发行包作为 Release 附件。模型权重由使用者在外部目录准备。

## 下载

- [运行依赖 ZIP](./story-video-runtime-v0.1.0.zip)：源码、配置示例、测试、环境安装入口、第三方说明。
- [FFmpeg Windows x64](https://github.com/mooncity0126/story-video-skill/releases/download/runtime-v0.1.0/ffmpeg-9.0.2-essentials-windows-x64.zip)：9.0.2 essentials 原始 ZIP，保留发行方许可证和说明。
- [Release](https://github.com/mooncity0126/story-video-skill/releases/tag/runtime-v0.1.0)：版本与附件。

下载运行依赖 ZIP 后解压，进入其中的 `story-video-skill` 文件夹。以下命令均在解压目录执行。

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
  .agents/skills/              后续 skill 的放置位置
```

`video-harness` 的主要依赖为 httpx、pydantic、Pillow、jsonschema；本地改写依赖 PyTorch 2.7.1 CUDA 11.8、Transformers 4.57.6、PEFT、Accelerate 和 bitsandbytes。两个环境各自使用 `requirements-lock.txt`，避免 Pillow 等版本约束互相影响。

## 接入后续 skill

把 skill 目录放入 `.agents/skills/`，使用生成的 `config/local-settings.json` 作为配置。它已绑定本仓库内的两个解释器、video-harness 源码、Rewriter 脚本、FFmpeg 和外部模型目录。默认保留本地改写、视频并发 4、图片并发 2。

媒体服务读取 `X2V_API_KEY`；可选文本/视觉服务读取 `QWEN_API_KEY`。密钥通过进程环境变量配置。

外部模型目录结构：

```text
Prompt-Rewriter/
  Qwen3-VL-8B-Instruct/
  MiniMax-H3-Prompt-Rewriter-LoRA-8B/
```

来源分别为 Qwen/Qwen3-VL-8B-Instruct 和 lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA-8B。本版本分发运行代码与安装清单，模型权重、历史生成结果和本机虚拟环境留在各自机器上。

## 验证与来源

发布前执行源码编译检查、离线测试、安装计划检查和压缩包完整性检查。本地 Rewriter 推理与真实云端生成未作为发布验证执行。

来源和打包调整见 `THIRD_PARTY_NOTICES.md`；具体测试结果随 Release 记录。FFmpeg 发行文件按 SHA256 校验。当前运行包以原 video-harness 的完整模块集合支持后续 skill；由 AI 主导的 skill 本体将另行加入。
