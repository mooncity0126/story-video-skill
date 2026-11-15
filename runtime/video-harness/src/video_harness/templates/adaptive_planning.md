把剧本自然地转换为分镜，保留故事、人物和对白，用简洁的画面与动作描述表达。镜头数量和节奏由剧情决定，并遵守配置的总时长预算及可选镜头数量上限。一个镜头表现一个完整叙事动作及自然反应，动作内的拿起、移动、放下通常是节拍。动作确实过密时允许在自然停顿处增加镜头，同时重新分配整片时长；不要把原镜头的全部时长复制给每个子镜头。角色引用输入角色 JSON 的 ID，外观沿用角色设定。普通布景无需逐物品建表，props 只记录有叙事或连续性意义的道具。

每镜分别描述起始画面、主要动作和结束状态，并设计合适的景别和运镜。视频提示词由后续 Rewriter 处理，此阶段 video_prompt 留 null，不写长篇模型指令或大量禁止事项。

start_state 会直接用于生成动作开始之前的静帧，应足以确定人物姿态、关键容器的数量和内容，以及马上要用的工具在哪里；不要只把这些信息写在 first_frame_description 或后续动作里。仅写本镜需要的关键状态，不列普通布景清单。time_jump 发生在本镜首帧之前，其省略的动作在本镜 continuity_notes 中说明。

按可拍摄的因果过程安排动作。每镜集中表现一个主要动作；拿起、倒入、放回、点火等涉及接触或物品状态变化的步骤，写清谁用什么、接触何处以及物品最后停在哪里。按真实节奏分配时间；多个不同操作过密，或剧情确有视角、时间、地点变化时，可以在自然停顿处分镜，并在总时长预算内安排各镜。人物只改变剧本要求的动作、表情或状态，服装、年龄和场景布局沿用设定；连续段落尽量采用稳定机位。道具 appearance 只写不随剧情变化的外观，点燃/熄灭、完整/破碎、盛装数量等写在各镜 start_state/end_state 中，避免把结果提前写进首帧。只记录关键物品，不扩展普通布景清单。

为程序安排生成次序，每镜提供 generation_plan：
- continuous：承接此前的动作或实际物品状态，references 引用相关更早镜头 tail。
- shared_reference：只需共享人物或场景外观；references 可为空，或者以 frame 引用更早的构图。
- independent：本镜起始状态能由剧本和公共参考独立确定，references 为空。
- uncertain：无法确定，程序保留串行。confidence 用 high 或 low，reason 用一句话解释。
回到先前场景时可以引用该场景最近的镜头，而非紧邻的另一场景。references 只指向已排在前面的镜头。人物相同不必然需要串行，连续操作则需要实际状态参考。

为程序计算时长，每镜提供 timing：把动作、对白和运镜拆成少量自然节拍，估计 duration_ms；after 列出必须先完成的 beat_id，确实同时进行的节拍可以不互相依赖。程序按最长依赖路径加起止停留、再按接口步长取整。超出单镜上限时在此阶段拆镜。每条对白对应一个 kind=dialogue 节拍，dialogue_index 为对白数组的索引；其他节拍为 null。对白沿用原剧本，未写出具体台词的解释可以用表演表达。

返回以下结构的 JSON（按剧情增减场景、道具、镜头和节拍；示例文字替换为实际内容）：
{
  "schema_version":"storyboard.v1", "status":"ok", "story_summary":"故事概要",
  "aspect_ratio":"9:16", "visual_style":"写实", "total_duration_ms":5000,
  "scenes":[{"scene_id":"SCENE_001","location":"地点","environment":"环境概述","layout_anchors":[],"lighting":"光线","time_of_day":null}],
  "props":[{"prop_id":"PROP_001","name":"关键道具","appearance":"简要外观","initial_state":"初始状态"}],
  "shots":[{
    "shot_id":"U001", "scene_id":"SCENE_001", "timeline_in_ms":0, "duration_ms":5000,
    "visible_characters":[{"character_id":"输入中的真实人物ID","variant_id":null}],
    "camera":{"shot_size":"中景","angle":"平视","movement":"固定","composition":"构图描述"},
    "first_frame_description":"开始的一幅画面", "start_state":"起始状态", "action_description":"主要动作", "end_state":"结束状态",
    "audio":{"dialogue":[],"sound_effects":[],"music":null}, "continuity_notes":[], "focus_prop_ids":[],
    "transition_from_previous":"first_shot", "first_frame_reference_policy":"planned_frame", "video_prompt":null,
    "generation_plan":{"relation":"independent","confidence":"high","reason":"首镜起始状态独立明确","references":[]},
    "timing":{"beats":[{"beat_id":"B1","description":"主要动作","duration_ms":4000,"after":[],"kind":"action","dialogue_index":null}],"start_hold_ms":0,"end_hold_ms":500,"reason":"动作加短暂停留"}
  }],
  "open_questions":[]
}

references 条目格式为 {"shot_id":"U001","kind":"tail","reason":"具体参考内容"}，kind 可为 tail/frame。
transition_from_previous 可为 first_shot/same_scene/scene_change/time_jump。
人物列表仅列画面可见者；audio.dialogue 每条为 {"speaker_character_id":"人物ID或null","text":"原文台词","delivery":"on_screen/off_screen/voice_over"}。
duration_ms 为正整数且不超过单镜上限，timeline_in_ms 连续累加。无人物/道具时数组可为空。
