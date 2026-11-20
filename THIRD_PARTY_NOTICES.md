# 来源与打包调整

## video-harness

运行模块取自用户 LightX2V 工作区的 video-harness 项目，包含接口客户端、参数契约、媒体处理、模板、测试与示例。LightX2V 来源为 https://github.com/ModelTC/LightX2V ，上游 Apache-2.0 文本保存在 `licenses/LightX2V-Apache-2.0.txt`。

发布副本将本机路径改为包内路径，移除了示例中指向历史运行文件的路径；原工作区代码保持原样。

## Prompt-Rewriter

本地 `rewrite.py` 与当前使用的 `rewrite_local.py` 是已有 Windows CUDA/NF4 运行适配，发布时将模型目录默认值改为包内 models 目录或 `STORY_VIDEO_MODEL_ROOT` 环境变量。模型权重作为外部输入。

官方提示词模板及来源说明取自：

- https://huggingface.co/lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA-8B
- 来源 revision：`a795219bd1677df34259eb4f3a77e2ec282e154f`
- 上游说明副本：`runtime/prompt-rewriter/upstream/README.md`
- 基座： https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct

第三方文件保留各自来源与适用授权，本次整理未为全部第三方内容重新指定统一许可证。

## FFmpeg

Release 附件为本地已有的 Gyan Windows x64 essentials 原始 ZIP，版本 `9.0.2-essentials_build`。附件内容保持原样。

- 发行方：https://www.gyan.dev/ffmpeg/builds/
- 原包声明：GPL v3
- 原包提供的 FFmpeg 源码：https://github.com/FFmpeg/FFmpeg/commit/946fcce07b
- 原始许可证和构建说明副本保存在 `licenses/FFmpeg-GPL-3.0.txt` 与 `licenses/FFmpeg-build-README.txt`。

Python 第三方包通过各自包仓库安装，版本分别锁定在两套环境的 `requirements-lock.txt`。

## drama-skills 创作方法

本仓库的 `produce-story-video` 在原有创作步骤中改编了 [zenstory-ai/drama-skills](https://github.com/zenstory-ai/drama-skills) 的剧本、对白、场景声音、整场分镜和空间调度方法，参考 revision 为 `8cf7404981b7095b498e061832bb3c9ffd6a42be`。

上游采用 MIT 许可证，`Copyright (c) 2026 drama-skills contributors`。完整许可证随可单独安装的 skill 保存在 [.agents/skills/produce-story-video/licenses/drama-skills-MIT.txt](.agents/skills/produce-story-video/licenses/drama-skills-MIT.txt)，逐项来源及适配范围见 [skill 来源说明](.agents/skills/produce-story-video/references/provenance.md)。本次整合不引入上游运行依赖或外部服务。

本次发布还采用 `zenstory-ai/drama-skills` 中的 `short-drama-video-prompts`，参考版本为 `37232f6d22e174c36e321d2e3a1a3ae871e7d602`。它提供镜头内表演、对白、运镜和声音的写作方法。MiniMax-H3 接口格式的来源见 [H3 提示词说明](.agents/skills/produce-story-video/references/minimax-h3.md)。
