import json
from pathlib import Path
import torch
import bitsandbytes as bnb
from transformers import Qwen3VLForConditionalGeneration
import peft

layer = bnb.nn.Linear4bit(128, 64, bias=False, compute_dtype=torch.float16, quant_type="nf4", compress_statistics=True)
layer = layer.to("cuda")
value = layer(torch.randn(1, 128, device="cuda", dtype=torch.float16))
torch.cuda.synchronize()
assert torch.isfinite(value).all()
report = {"torch": torch.__version__, "cuda_runtime": torch.version.cuda, "gpu": torch.cuda.get_device_name(), "capability": torch.cuda.get_device_capability(), "bitsandbytes": bnb.__version__, "peft": peft.__version__, "nf4_cuda_test": "passed", "transformers_qwen3vl_class": Qwen3VLForConditionalGeneration.__name__}
print(json.dumps(report, indent=2))
out = Path(__file__).resolve().parent / "outputs" / "runtime-check.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
