# MiniMax H3 直接提示词

本文件适配 [MiniMax 官方 H3 提示词指南](https://github.com/MiniMax-AI/MiniMax-H3/blob/199b7d0e1fd95e7b3eafa8c15eca692534142427/skills/h3-prompt-writing/references/base-en.txt)，配合 [video-prompts.md](video-prompts.md) 的动作与表演方法使用。当前 AI 按以下规范写稿。直写原样提交，本地或在线路线按[本片设置](first-use.md)处理。

## 当前接口与输出格式

当前 LightX2V 的 `MiniMax-H3 / i2av` 接收首帧 `input_image`，可选尾帧 `input_last_frame`，生成原生音画。以本次接口规格为准；现有规格为 **5–15 秒整数**。官方其他模式的时长和素材能力不能直接当作当前网关的能力。

画面、表演、运镜和声音描述用英文，中文台词原字保留在 `<d>[Chinese] …</d>` 中。首帧/首尾帧模式只用**对齐指令加三个核心字段**；不是全参考模式的六段模板。`[Shot 1]` 是本次生成内部镜号，不是项目的 `S001` 人物或任务编号。

**单首帧 I2VA：**以下对齐指令必须独占首行，之后空一行，再写三个核心字段。下面是可直接使用的 5 秒例子；真实任务替换为本镜内容。

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] Live-action, a static medium close-up matching the first frame. The man with a low, slightly husky Mandarin voice (S1) stands beside the woman at the desk. Determined to take responsibility, he turns his evasive glance into direct eye contact and tightens his jaw as he begins speaking at about 0.30 seconds. He says, with restrained tension on the first clause and firm emphasis on the second, taking one brief pause at the comma: <d>[Chinese] 这笔钱是我拿的，和她没有关系。</d> His complete line ends by 3.80 seconds. On hearing the admission, the woman stops her hand above the papers and looks directly at him, her lips closed. He holds her gaze with a steady posture through the ending. Keep at least 0.90 seconds after his last syllable; no additional words or background voices. The camera remains static throughout the 5-second clip.

overall_soundscape: Low, steady room tone and a brief sleeve rustle accompany the movement. The dialogue stays clear in the foreground.

non_diegetic_music: N/A
```

例中对白 13 个实读汉字，净说话约 `13 / 4.1 = 3.17` 秒；加起声和一次短停顿后安排在 3.80 秒前结束。语气和身体变化与说话重叠，不在开口前另加长凝视。

**首尾帧 FL2VA：**改用下面的首行，其余仍为三个核心字段。例中为 6 秒单镜，真实任务将末尾镜号替换为实际最后一个内部镜号，将 `6.00` 替换为本次请求秒数，保留两位小数。

```text
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the 6.00-second mark of the target video.
```

明确写出从首帧到尾帧的动作路径、变化和落定，不只写“变成第二张图”。首尾帧优先连续单镜；要切镜时仍必须合理到达真实目标尾帧，不能让尾帧与最终机位矛盾。

## 三个字段如何分工

1. `integrated_multimodal_description`：从 `[Shot 1]` 开始，首镜不在镜号后加切镜时间。按时间顺序写动作、表演变化、运镜、准确的说话人与台词、同步动作声和结尾状态。说话人的身份、年龄/性别、音质、口音、动作及语气写在 `<d>` 外；标签内只放语种和原文，连标点也保留。
2. `overall_soundscape`：用 1–4 句连贯英文概述底声、物理动作声与有意义的非语言人声，不重复对白。只有全片明确要求完全静音时才写 `N/A`；无配乐不等于无环境声。
3. `non_diegetic_music`：未要求模型生成背景配乐时写 `N/A`。画内手机铃声、收音机、现场弹奏等实际声源放在第一个字段，注明发生时机。

每个说话人使用稳定的 `(S1)`、`(S2)`，同一角色跨镜沿用同一编号。每份独立请求都带足够的身份与稳定声音描述，模型不会读取上一请求的文字。中文台词只写一遍，禁止换成英文翻译、概述或“he speaks his line”；也不能把制作说明、角色标签或情绪括注塞进台词标签。

画内对白明确口型属于谁。画外同期声说明同场声源方位，不冒充内心独白。旁白使用官方短语 `says in an off-screen voiceover`；每个旁白 `<d>` 后紧接说明对应画内人物嘴唇闭合。旁听者不跟着念词。对话不是群体齐声时，不使用合并说话人编号。

## 有理由的内部切镜

普通片段保持一个清晰表演单元；当 1–3 秒的反应/证据镜头独立生成会凑长时，可以在同一请求中安排少量内部切镜，通常 2–3 个已经足够。先在分镜确定切点和作用，再转成官方写法：

```text
[Shot 2] At 00:03.500, the camera cuts to a close-up of the listener as she recognizes the signature on the paper.
```

后续镜号连续，切镜时间严格递增且早于总时长。切镜必须呈现证据、后果、关系变化或新的视点；不能为提速每半秒换机位，也不必每句台词都正反打。固定机位可有丰富表演，不能默认插入慢推。

同一次生成中，若一句台词必须跨内部切点，用官方 `<scenetrans>` 在两部分连接处标记并说明声音连续；按原文分配前后半句，不重复全句。优先选择句间或自然语义停顿切镜，能避免跨句标记就不额外使用。**两个独立请求之间不跨句**：它们不共享连续语音。每个生成片段的最后一句只预留一次 0.8–1 秒余量，内部切点不重新叠加起声和尾部空白。

内部切镜是模型生成指令，不是本地确定性的后期剪辑；仍作为一个任务完整检查和合成，不增加调用或检查次数。当前输入只绑定真实首帧/可选尾帧，不能虚构每个内部镜头都有独立挂图字段。需要更多时间或不同素材条件时，在自然边界拆成独立请求。

## 能力边界

- H3 的全参考 Ref2VA、参考音频和六字段格式属于其他模式；当前接口未暴露这些输入，不通过提示词伪装成已传入音色样本。稳定音色文字是约束，不保证跨请求声纹相同。
- 4.1 字/秒是本项目采用的普通中文估时基准，不保证每次生成严格达成。必要情绪停顿计入规划，生成的实际余长不能提前当作句尾预算。
- 上游提到的开口延迟来自特定样本，不给每个镜头机械增加数秒空白；直接对白尽早开口，情绪反应与说话自然重叠，实测确有延迟时再调整后续规划。
- 不用 `<cutoff>` 掩盖镜长不足，不为压缩时长截断句子。完整原生音轨合成和原有检查轮次继续有效。

具体接入来源与版本见 [provenance.md](provenance.md)。
