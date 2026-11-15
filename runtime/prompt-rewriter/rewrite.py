"""Local H3 rewriting with optional real keyframes; 4-bit base + LoRA."""
from __future__ import annotations
import argparse
import importlib.metadata
import hashlib
import json
import os
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
MODEL_ROOT = Path(os.environ.get("STORY_VIDEO_MODEL_ROOT", str(PROJECT.parents[1] / "models" / "Prompt-Rewriter")))
# Set these before importing any Hugging Face libraries. No remote fallback.
os.environ["HF_HOME"] = str(MODEL_ROOT / "hf-cache")
os.environ["HF_HUB_CACHE"] = str(MODEL_ROOT / "hf-cache" / "hub")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:128")

def arguments():
    p = argparse.ArgumentParser(description="MiniMax-H3 文字/参考帧改写（本地 4-bit + LoRA）")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--prompt")
    source.add_argument("--prompt-file", type=Path)
    p.add_argument("--duration", type=int, choices=range(4, 16), default=5)
    p.add_argument("--task", choices=("t2av", "i2av", "l2av", "fl2av"), default="t2av")
    p.add_argument("--first-frame", type=Path)
    p.add_argument("--last-frame", type=Path)
    p.add_argument("--image-max-pixels", type=int, default=262144)
    p.add_argument("--resolution", choices=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"), default="16:9")
    p.add_argument("--max-new-tokens", type=int, default=768)
    p.add_argument("--max-input-tokens", type=int, default=4096)
    p.add_argument("--output", type=Path, default=PROJECT / "outputs" / "latest.json")
    p.add_argument("--placement", choices=("balanced", "gpu"), default="balanced", help="balanced keeps vision, embeddings, output head and some decoder layers in RAM")
    p.add_argument("--cpu-layers", type=int, default=4, choices=range(0, 13), help="First decoder layers placed in RAM; used with balanced placement")
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--model-root", type=Path, default=MODEL_ROOT)
    p.add_argument("--system-suffix-file", type=Path, help="Optional local integration instructions appended after the official system prompt")
    args = p.parse_args()
    if args.prompt_file:
        args.prompt = args.prompt_file.read_text(encoding="utf-8-sig").strip()
    elif not args.prompt:
        args.prompt = input("请输入需要扩写的提示词：").strip()
    if not args.prompt:
        p.error("提示词不能为空")
    if not 1 <= args.max_new_tokens <= 1024:
        p.error("此 8GB 配置要求 --max-new-tokens 在 1–1024 之间")
    if not 1 <= args.max_input_tokens <= 8192:
        p.error("--max-input-tokens 必须在 1–8192 之间")
    expected = {"t2av": (False, False), "i2av": (True, False), "l2av": (False, True), "fl2av": (True, True)}[args.task]
    if (bool(args.first_frame), bool(args.last_frame)) != expected:
        p.error("图像参数必须与 task 的首帧/尾帧角色一致")
    if not 65536 <= args.image_max_pixels <= 1048576:
        p.error("--image-max-pixels 必须在 65536–1048576 之间")
    for path in (args.first_frame, args.last_frame):
        if path and not path.is_file():
            p.error(f"参考帧不存在：{path}")
    return args

def main():
    args = arguments()
    model_root = args.model_root.resolve()
    os.environ["HF_HOME"] = str(model_root / "hf-cache")
    os.environ["HF_HUB_CACHE"] = str(model_root / "hf-cache" / "hub")
    import torch
    from peft import PeftModel
    from accelerate import dispatch_model
    from accelerate.hooks import remove_hook_from_module
    from transformers import AutoProcessor, BitsAndBytesConfig, Qwen3VLForConditionalGeneration, TextStreamer
    from prompt_template import build_messages
    from PIL import Image, ImageOps

    if not torch.cuda.is_available():
        raise RuntimeError("没有检测到可用 CUDA GPU，请检查驱动和项目解释器")
    base = model_root / "Qwen3-VL-8B-Instruct"
    adapter = model_root / "MiniMax-H3-Prompt-Rewriter-LoRA-8B"
    for required in (base / "model.safetensors.index.json", adapter / "adapter_model.safetensors"):
        if not required.is_file():
            raise FileNotFoundError(f"模型文件尚未下载完成：{required}")
    torch.manual_seed(42)
    torch.set_num_threads(min(8, os.cpu_count() or 4))
    torch.cuda.reset_peak_memory_stats()
    free_before, total = torch.cuda.mem_get_info()
    started = time.perf_counter()
    processor = AutoProcessor.from_pretrained(str(base), local_files_only=True, trust_remote_code=False, min_pixels=65536, max_pixels=args.image_max_pixels)
    messages = build_messages(args.prompt, task=args.task, resolution=args.resolution, duration=args.duration)
    suffix = args.system_suffix_file.read_text(encoding="utf-8-sig").strip() if args.system_suffix_file else ""
    if suffix:
        messages[0]["content"] += "\n\n" + suffix
    rendered = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    images, frame_records = [], []
    for role, path in (("first_frame", args.first_frame), ("last_frame", args.last_frame)):
        if path:
            with Image.open(path) as image:
                images.append(ImageOps.exif_transpose(image).convert("RGB").copy())
            frame_records.append({"role": role, "path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": list(images[-1].size)})
    inputs = processor(text=[rendered], return_tensors="pt", padding=False, **({"images": images} if images else {}))
    # Transformers 4.57 Qwen3-VL does not consume this newer multimodal
    # argument. Image roles use image_grid_thw and the image placeholder tokens.
    inputs.pop("mm_token_type_ids", None)
    input_tokens = inputs["input_ids"].shape[1]
    if input_tokens > args.max_input_tokens:
        raise ValueError(f"完整输入（含官方系统提示词）为 {input_tokens} tokens，超过上限 {args.max_input_tokens}。请缩短用户输入。")
    print(f"GPU: {torch.cuda.get_device_name(0)}; free {free_before/2**30:.2f}/{total/2**30:.2f} GiB; input {input_tokens} tokens", flush=True)
    if args.placement == "balanced":
        device_map = {
            "model.visual": "cpu",
            "model.language_model.embed_tokens": "cpu",
            "model.language_model.norm": 0,
            "model.language_model.rotary_emb": 0,
            "lm_head": "cpu",
        }
        for layer in range(36):
            device_map[f"model.language_model.layers.{layer}"] = "cpu" if layer < args.cpu_layers else 0
    else:
        device_map = {"": 0}
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.float16, llm_int8_enable_fp32_cpu_offload=True)
    print("Loading 4-bit base; balanced placement leaves room for the rank-256 LoRA...", flush=True)
    model = Qwen3VLForConditionalGeneration.from_pretrained(str(base), local_files_only=True, trust_remote_code=False, torch_dtype=torch.float16, quantization_config=quant, device_map=device_map, low_cpu_mem_usage=True, attn_implementation="sdpa")
    # PEFT otherwise defaults to a fresh automatic map and can choose disk
    # offload despite the deliberate 8GB map above. Its wrapper adds this prefix.
    peft_device_map = {f"base_model.model.{name}" if name else "": device for name, device in device_map.items()}
    model = PeftModel.from_pretrained(
        model, str(adapter), local_files_only=True, is_trainable=False,
        autocast_adapter_dtype=False, low_cpu_mem_usage=True,
        torch_device="cpu", device_map=peft_device_map,
    )
    if args.placement == "balanced":
        # Standard CPU offload executes RAM modules on the GPU, temporarily
        # staging the 1.2GB output head. Instead compute these modules on CPU.
        # Detach old hooks first so any meta-backed RAM weights are restored.
        # PEFT wrapper __getattr__ delegates to its child; only detach hooks
        # actually owned by each module, avoiding a delegated-attribute error.
        for module in model.modules():
            if "_hf_hook" in vars(module):
                remove_hook_from_module(module)
        model = dispatch_model(model, device_map=peft_device_map, main_device="cpu")
        if images:
            # CPU vision kernels use float32; keep decoder's established map.
            model.get_base_model().model.visual.float()
    model.eval()
    active = model.active_adapters
    lora_parameters = [(name, value) for name, value in model.named_parameters() if "lora_" in name]
    if not active or not lora_parameters or not getattr(model, "is_loaded_in_4bit", False):
        raise RuntimeError("量化或 LoRA 激活校验失败")
    first_b = next(value for name, value in lora_parameters if "lora_B" in name)
    if not bool(torch.count_nonzero(first_b.detach()).item()):
        raise RuntimeError("LoRA B 权重全为零，未检测到训练后的 adapter")
    input_device = model.get_input_embeddings().weight.device
    inputs = {key: value.to(input_device) if isinstance(value, torch.Tensor) else value for key, value in inputs.items()}
    torch.cuda.synchronize()
    loaded = time.perf_counter()
    print(f"Loaded in {loaded-started:.1f}s; active adapters: {active}; LoRA tensors: {len(lora_parameters)}; generating...", flush=True)
    streamer = None if args.no_stream else TextStreamer(processor.tokenizer, skip_prompt=True, skip_special_tokens=True)
    with torch.inference_mode():
        ids = model.generate(**inputs, max_new_tokens=args.max_new_tokens, do_sample=False, temperature=None, top_p=None, top_k=None, use_cache=True, streamer=streamer)
    torch.cuda.synchronize()
    finished = time.perf_counter()
    generated = ids[0, input_tokens:]
    text = processor.decode(generated, skip_special_tokens=True).strip()
    if not text:
        raise RuntimeError("模型返回空白输出")
    fields = ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:")
    field_positions = [text.find(field) for field in fields]
    format_valid = all(position >= 0 for position in field_positions) and field_positions == sorted(field_positions)
    ended = int(generated[-1]) in (model.generation_config.eos_token_id if isinstance(model.generation_config.eos_token_id, list) else [model.generation_config.eos_token_id])
    payload = {
        "task": args.task, "resolution": args.resolution, "duration": args.duration,
        "prompt": args.prompt, "first_frame": str(args.first_frame.resolve()) if args.first_frame else None, "last_frame": str(args.last_frame.resolve()) if args.last_frame else None,
        "reference_frames": frame_records, "image_max_pixels": args.image_max_pixels,
        "image_grid_thw": inputs["image_grid_thw"].tolist() if "image_grid_thw" in inputs else None,
        "base_model": str(base), "adapter_path": str(adapter), "enhanced_prompt": text,
        "format_valid": format_valid,
        "system_prompt_file_sha256": hashlib.sha256((PROJECT/"prompt_template.py").read_bytes()).hexdigest(),
        "integration_system_suffix": suffix,
        "metrics": {"load_seconds": round(loaded-started, 3), "generation_seconds": round(finished-loaded, 3), "total_seconds": round(finished-started, 3), "input_tokens": input_tokens, "generated_tokens": len(generated), "tokens_per_second": round(len(generated)/(finished-loaded), 3), "peak_allocated_mib": round(torch.cuda.max_memory_allocated()/2**20, 1), "peak_reserved_mib": round(torch.cuda.max_memory_reserved()/2**20, 1), "finished_with_eos": ended, "max_new_tokens": args.max_new_tokens},
        "runtime": {"gpu": torch.cuda.get_device_name(0), "torch_cuda": torch.version.cuda, "placement": args.placement, "cpu_layers": args.cpu_layers if args.placement == "balanced" else 0, "quantization": "NF4 double quantization on GPU linear layers, FP16 compute; CPU modules are not quantized", "active_adapters": active, "lora_tensor_count": len(lora_parameters), "lora_dtype": str(lora_parameters[0][1].dtype), "versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "peft", "accelerate", "bitsandbytes")}},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    args.output.with_suffix(".txt").write_text(text+"\n", encoding="utf-8")
    if args.no_stream:
        print(text)
    print(f"\nSaved: {args.output}\nMetrics: {json.dumps(payload['metrics'], ensure_ascii=False)}", flush=True)
    if not ended:
        print("提示：达到输出 token 上限，文本可能未完成；可使用 --max-new-tokens 1024 重试。", flush=True)
    if not format_valid:
        print("提示：输出未包含全部三个 H3 字段，请检查文本或增大输出上限。", flush=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
