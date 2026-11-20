# 原子操作

AI 根据已完成的产物选择下一条命令；每条命令完成一次具体操作。使用配置指定的 Python：

```powershell
$projectRoot = (Get-Location).Path
$videoPython = Join-Path $projectRoot 'runtime\video-harness\.venv\Scripts\python.exe'
$videoTool = Join-Path $projectRoot 'scripts\media.py'
$videoRun = Join-Path $projectRoot 'runs\short-story-01'
& $videoPython $videoTool doctor
& $videoPython $videoTool setup
# 首次在工作台保存偏好后建片
& $videoPython $videoTool init --output $videoRun
```

新片首次先执行 `setup`，向用户展示选项及影响，取得确认再写入偏好。完整用法见[首次设置](first-use.md)。`doctor` 检查所读配置、工具路径与凭据是否存在，并返回当前提示词模式；停用本地改写时不检查其环境或权重。`init` 创建项目目录和 `settings.json`。AI 填入已确定的预算与规格，并保存剧本、分镜、每镜用时依据、依赖和检查选择；纯配置与文档验证使用这些本地操作。

## 模型规格、提交与查询

```powershell
& $videoPython $videoTool --run $videoRun specs --role video
& $videoPython $videoTool --run $videoRun submit --request "$videoRun\requests\frame-S001.json"
& $videoPython $videoTool --run $videoRun submit --request "$videoRun\requests\frame-S001.json" --execute
& $videoPython $videoTool --run $videoRun poll --job frame_S001_v1
& $videoPython $videoTool --run $videoRun status
```

| 操作 | 输入 | 输出 |
|---|---|---|
| `specs` | 角色 `image/edit/video` 与模型配置 | 该配置的规格缓存 |
| `submit` | 请求 JSON | 校验结果与报价 |
| `submit --execute` | 请求 JSON、已设置预算、并发名额 | 持久化任务记录和云端 task_id，随即返回 |
| `poll` | 已知 job_id | 查询一次；完成时下载素材、保存媒体信息 |
| `status` | 本项目任务目录 | 各任务状态、积分、分类并发占用/上限/可用名额 |

`image` 与 `edit` 共用 2 个并发名额，视频串行上限 1 个且须逐镜承接实际尾帧，按依赖并行时默认最多 4 个，总上限 6 个。AI 在云端运行期间准备其他就绪镜头，并在任一名额释放后提交下一个就绪任务。同一 job_id 与相同输入返回已有记录。

请求中的图片、提示词文件与改写记录路径相对请求 JSON 所在目录解析，也支持绝对路径。首帧图片请求：

```json
{
  "job_id": "frame_S001_v1",
  "role": "edit",
  "shot_id": "S001",
  "prompt": "依据参考人物与房间，呈现人物尚未拿起桌上杯子的起始画面。",
  "inputs": {"input_image": ["../references/character.png", "../references/room.png"]}
}
```

`image` 生成设定图；`edit` 用参考图生成/编辑首帧或目标尾帧。目标尾帧按关键结束状态生成，通常以本镜首帧为编辑输入。可选帧检查的选择和结果由 AI 按工作流保存。

例如第二镜承接第一镜放卡的动作，在第二镜的依赖中填写 `{"kind":"actual_tail","source_id":"S001","reason":"黑卡的位置与持有者延续"}`。采用第一镜视频并保存该候选的检查结果，调用 `tail` 取得完整源素材末帧。把返回的图片、本场空景和本镜定妆图一并传给首帧编辑任务。保存实际编辑来源后，将生成的本镜首帧填入视频的 `input_image`。工作台、直接提交和新时间线的合成都核对这些来源。相同历史时间线可使用已交付的候选及其检查记录重新合成。

## AI 最终提示词与视频（默认）

当前 AI 读取 [video-prompts.md](video-prompts.md) 与 [minimax-h3.md](minimax-h3.md)，将定稿分镜、真实首帧/选用的目标尾帧转为 `prompts/S001-h3.txt`。文件只含最终模型正文，逐字对白、情绪表演、说话窗口和句尾余量均写入；时长按实读字数、4.1 字/秒基准、必要停顿与动作重叠计算，不固定为统一镜长。

直写模式下确认本次设置为 `rewriter.enabled: false` 且 H3 参数 `enhance_prompt: false`。视频请求直接引用该文件，不填写 `rewriter_record`：

```json
{
  "job_id": "video_S001_v1",
  "role": "video",
  "shot_id": "S001",
  "duration_ms": 5000,
  "prompt_file": "../prompts/S001-h3.txt",
  "inputs": {"input_image": ["../assets/frame_S001_v1.image"]}
}
```

文件按 UTF-8 读取，正文原样进入报价/提交内容和任务记录，不调用本地模型或外部文字服务。选用目标尾帧时增加 `input_last_frame`，同时使用 H3 首尾帧对齐指令。请求、分镜、实际图片与提示词中时长/起止状态保持一致。完成接口参数和预算校验后沿用 `submit --execute`、`poll`，不增加声音或提示词检查轮次。

在线优化沿用上面的 `prompt_file` 请求，快照中的 `rewriter.mode` 为 `online` 且 `enhance_prompt` 为 `true`，平台在生成时处理。续做读取影片现有快照。

## 选用本地改写时

本片选择本地改写时，`rewriter.enabled` 为 `true`。工作台按实际素材运行改写。对话端可使用以下命令。启用时仍保持 `enhance_prompt: false`，实际图片、时长和画幅必须绑定同一输入。

```powershell
& $videoPython $videoTool --run $videoRun rewrite --prompt-file "$videoRun\prompts\S001-source.txt" --duration-ms 5000 --first-frame "$videoRun\assets\frame_S001_v1.image"
```

选择目标尾帧时增加 `--last-frame`。输入为真实图片和源提示词，输出为 `record_path` 与 `prompt_file`。可选帧检查启用时，AI 将其观察事实写入源提示词后调用改写。视频请求引用命令实际返回的记录路径：

```json
{
  "job_id": "video_S001_v1",
  "role": "video",
  "shot_id": "S001",
  "duration_ms": 5000,
  "rewriter_record": "../prompt_rewrites/本次返回的缓存目录/record.json",
  "inputs": {"input_image": ["../assets/frame_S001_v1.image"]}
}
```

本地模式的视频正文取自保存的改写记录。选用目标尾帧时提供 `input_last_frame`，与改写时图片保持相同内容哈希。脚本核对图片、时长、画幅和原始改写文件；本地改写由适配器单任务锁执行。若已有按需 Prompt 检查发现严重问题，AI 在既有返工额度内修正源输入并保存新版本。

## 视频采样与结论

```powershell
& $videoPython $videoTool --run $videoRun inspect --job video_S001_v1
```

`inspect` 输出视频路径、6 张均匀采样、时间戳联系表和完整原生音轨；采样包含完整源视频的首末帧，不在请求时长处截断。同一候选复用缓存与已有结论，不为片尾追加声音检查。AI 查看本轮材料后写入一份报告，`source_sha256` 取返回的真实值：

```json
{
  "source_sha256": "inspect返回的视频sha256",
  "outcome": "uncertain",
  "notes": "采样中的人物和场景一致；关键手部接触发生在采样间隔内。",
  "coverage": "查看6张带时间戳采样，覆盖0秒至4.96秒；音轨未听取。",
  "audio_checked": false
}
```

报告的采样时间按本次实际结果填写。严重问题在 `notes` 中包含时间点、预期与观察事实、修正方向。

```powershell
& $videoPython $videoTool --run $videoRun review --job video_S001_v1 --report "$videoRun\reviews-input\S001.json"
& $videoPython $videoTool --run $videoRun tail --job video_S001_v1
```

`review` 保存 AI 或配置 VLM 的结论；`pass/uncertain` 可采用候选，`serious` 进入返工节点。`tail` 提取完整源视频的实际末帧，返回 `path`、来源视频哈希、`requested_duration_ms`、`retained_duration_ms`（完整音画时长）、`retained_video_duration_ms` 和 `retention_policy: full_source`。新依赖使用返回的 `*-tail-full.png` 路径；旧 `*-tail.png` 及既有依赖哈希保持不变。

内容返工先更新 `repair-ledger.json`。替换视频使用新 job_id，并加 `"repair_of":"video_S001_v1"`；AI 管理每镜跨节点共享的 1 次返工，脚本执行视频返工上限。新候选检查 1 次，相同候选复用结论。AI 按哈希更新受影响的下游依赖。

明确失败或经核实未提交的技术任务可用 `retry_of` 指向原任务，技术重试最多 1 次；技术重试次数与内容返工次数分别记录。

## 可选外部文本/视觉调用

当前 AI 或外部模型二选一完成对应检查。外部模型每次接收该节点的材料，输出 JSON：

```powershell
& $videoPython $videoTool --run $videoRun text --provider vision --instructions "$videoRun\prompts\video-review.txt" --input "$videoRun\prompts\S001-contract.txt" --images "$videoRun\inspection\video_S001_v1\contact-sheet.jpg" --output "$videoRun\reviews-input\S001-raw.json"
```

帧检查用同一次 `text --images` 提交实际首帧及选用的目标尾帧。视频检查需要的起止参考也可放在同一次请求中。AI 将外部结论与本地取得的真实来源哈希合并，写入 `review` 报告；调用次数和 token 使用量按配置记录。

## 合成与恢复

AI 按播放顺序写 `timeline.json`，如 `[{"job_id":"video_S001_v1"},{"job_id":"video_S002_v2"}]`。`duration_ms` 是生成请求时长，不是裁切时长；每镜合成完整源音画。若条目另含 `duration_ms`，它必须与原请求相同，不能用于截短素材。

```powershell
& $videoPython $videoTool --run $videoRun assemble --timeline "$videoRun\timeline.json"
```

输出 `exports/final.mp4` 和 `exports/assembly.json`，完整保留各镜原生音轨，不按计划时长裁切或加速。声音比画面长时延续最后一帧，等该镜声音结束再接下一镜，不叠压对白。报告分别保存 `planned_duration_ms`、`source_total_duration_ms`、`actual_duration_ms`、采用任务及原有检查结论。

`production.target_duration_ms` 用于核对请求时长合计；可选的 `production.max_duration_ms` 是完整音画的用户上限。超过上限时返回调整分镜/台词的说明，不裁音轨或覆盖旧成片。旧候选重合成沿用原检查结论，报告注明原采样未覆盖新增保留片尾。仍只执行原有的成片技术校验，不增加转写、对词或声音检查。

`submission_unknown` 进入核实状态：取得已提交任务 ID 后，用 `resolve --job ID --task-id 实际ID --evidence 核实依据` 关联原任务；确认未产生任务时，用 `--confirmed-not-submitted` 保存依据，再决定一次技术重试。

`PAUSE_REQUESTED` 将本项目置为暂停：已有任务可查询和保存结果；用户恢复指令到达后恢复新提交与改写。AI 在 `queue.json` 保存下一步、等待产物与待决定事项。

## 离线验证

测试材料为模拟 HTTP 服务和 FFmpeg 生成的本地片段；测试夹具屏蔽真实网络与本地 Rewriter 推理入口。

```powershell
& $videoPython -m pytest "$videoSkill\scripts\test_media_tools.py" -q
```
# 工作台的生成前确认

`submit --execute` 新建视频任务前要求当前素材版本的用户确认；先按 [material-review.md](material-review.md) 展示本地工作台。图片/报价不受该人工确认限制，但仍受原有暂停及预算规则约束。`workflow-inbox.json` 有待规划输入时也阻止新视频提交。不能代用户确认或自行关闭确认设置。
