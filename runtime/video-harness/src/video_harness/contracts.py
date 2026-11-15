from __future__ import annotations

import math

from jsonschema import Draft202012Validator

from .util import HarnessError


class Contract:
    def __init__(self, spec, openapi):
        self.spec, self.openapi = spec, openapi
        self.params, self.inputs = spec["parameters"], spec["inputs"]

    def validate(self, body, quote=False):
        name = "QuoteRequest" if quote else "TaskSubmitRequest"
        branches = self.openapi["components"]["schemas"][name]["oneOf"]
        matches = [b for b in branches if all(b["properties"].get(k, {}).get("const", body[k]) == body[k] for k in ("task", "model_cls", "stage"))]
        if len(matches) != 1:
            raise HarnessError("OpenAPI 中无法唯一确定所选模型契约")
        schema = {**matches[0], "components": self.openapi["components"]}
        errors = list(Draft202012Validator(schema).iter_errors(body))
        if errors:
            # jsonschema's error.message can contain full base64 input. Only emit field paths.
            paths = [".".join(map(str, e.absolute_path)) or "request" for e in errors[:5]]
            raise HarnessError("OpenAPI 请求校验失败: " + ", ".join(paths))

    def parameters(self, choice, prompt, aspect_ratio, duration_ms=None):
        result = {"task": choice.task, "model_cls": choice.model_cls, "stage": choice.stage, "wait": False}
        unknown = set(choice.parameters) - set(self.params)
        if unknown:
            raise HarnessError("模型不支持参数: " + ", ".join(sorted(unknown)))
        result.update(choice.parameters)
        if "prompt" in self.params:
            result["prompt"] = prompt
        if "aspect_ratio" in self.params and "aspect_ratio" not in result:
            opts = self.params["aspect_ratio"].get("options", [])
            if aspect_ratio in opts or not opts:
                result["aspect_ratio"] = aspect_ratio
            elif opts == ["adaptive"]:
                result["aspect_ratio"] = "adaptive"
            else:
                raise HarnessError("模型不支持项目画幅；请显式选择支持该画幅的模型")
        duration_spec = self.spec.get("output", {}).get("duration", {})
        if duration_ms is not None:
            field = duration_spec.get("parameter")
            if field:
                p = self.params.get(field)
                if not p:
                    raise HarnessError("模型时长规格不完整")
                target = duration_ms / 1000
                value = max(target, p.get("minimum", target))
                if p["type"] == "integer":
                    value = math.ceil(value)
                options = p.get("options")
                if options:
                    eligible = [x for x in options if x >= value and x <= 15]
                    if not eligible:
                        raise HarnessError("没有能覆盖该镜且不超过15秒的模型时长")
                    value = min(eligible)
                if value > min(15, p.get("maximum", 15)):
                    raise HarnessError("镜头时长不符合模型或15秒限制，请在规划阶段拆镜")
                if field in result and result[field] != value:
                    raise HarnessError("模型时长由镜头规划适配，请从固定 parameters 中删除时长字段")
                result[field] = value
            # A fixed/unspecified duration model gets NO invented duration field.
        for key, p in self.params.items():
            available = all(result.get(c["field"], self.params.get(c["field"], {}).get("default")) != c.get("not_equals") for c in p.get("available_when", []))
            if key in result and not available:
                raise HarnessError(f"模型参数条件不满足: {key}")
            required = p.get("required", False) or any(result.get(c["field"], self.params.get(c["field"], {}).get("default")) == c["equals"] for c in p.get("required_when", []))
            if key not in result and required:
                if "default" in p and available:
                    result[key] = p["default"]
                else:
                    raise HarnessError(f"缺少模型必需参数: {key}")
            if key in result:
                schema = {k: v for k, v in p.items() if k in {"type", "minimum", "maximum", "properties"}}
                # Nested parameter descriptors are not JSON Schema; root OpenAPI verifies objects.
                schema.pop("properties", None)
                if "options" in p:
                    schema["enum"] = p["options"]
                if list(Draft202012Validator(schema).iter_errors(result[key])):
                    raise HarnessError(f"模型参数值不受支持: {key}")
        return result

    def check_references(self, field, metas):
        spec = self.inputs.get(field)
        if not spec or spec["type"] != "image":
            raise HarnessError(f"所选模型没有图像输入字段 {field}")
        maximum = spec.get("maximum", 1 if not spec["multiple"] else 999)
        minimum = spec.get("minimum", 0)
        if not minimum <= len(metas) <= maximum:
            raise HarnessError(f"需要 {len(metas)} 张参考图，模型上限 {maximum}；请配置参考图容量足够的编辑模型，例如当前支持16图的 gpt-image-2.5-flare，不能丢弃人物参考")
        if len(metas) > 1 and not spec["multiple"]:
            raise HarnessError("模型不支持多参考图")
        for meta in metas:
            ratio = meta["width"] / meta["height"]
            limits = spec.get("aspect_ratio", {})
            if ratio < limits.get("minimum", 0) or ratio > limits.get("maximum", float("inf")):
                raise HarnessError("输入图片比例不符合模型限制")

    def with_media(self, body, field, values, metas):
        self.check_references(field, metas)
        body = dict(body)
        body[field] = values if self.inputs[field]["multiple"] else values[0]
        self.validate(body)
        return body

    def quote_body(self, body, metas, *, last_frame_meta=None):
        result = dict(body)
        if metas:
            result["input_meta"] = {"images": [{"width": m["width"], "height": m["height"]} for m in metas]}
            if len(metas) == 1:
                result["input_meta"]["image"] = result["input_meta"]["images"][0]
        if last_frame_meta is not None:
            if "last_frame" not in self.openapi["components"]["schemas"]["InputMeta"]["properties"]:
                raise HarnessError("当前报价契约不支持独立尾帧尺寸")
            result.setdefault("input_meta", {})["last_frame"] = {"width": last_frame_meta["width"], "height": last_frame_meta["height"]}
        self.validate(result, quote=True)
        return result

    def expected_image_meta(self, body):
        output = self.spec.get("output", {})
        dims = output.get("resolutions", {})
        level = body.get("resolution_level", self.params.get("resolution_level", {}).get("default"))
        if level in dims:
            dims = dims[level]
        ratio = body.get("aspect_ratio", self.params.get("aspect_ratio", {}).get("default"))
        dims = dims.get(ratio) if isinstance(dims, dict) else None
        if isinstance(dims, list) and len(dims) == 2:
            # Official specs encode resolution as [height, width]. Estimates are labelled in dry-run.
            return {"width": dims[1], "height": dims[0], "estimated": True}
        return None


def discover(client, config):
    client.models()
    openapi = client.openapi()
    contracts = {}
    for role in ("image", "edit", "video", "image_fallback", "edit_fallback"):
        choice = getattr(config, role)
        if choice is None:
            continue
        specs = client.specs(choice)
        matches = [s for s in specs if (s["task"], s["model_cls"], s["stage"]) == (choice.task, choice.model_cls, choice.stage)]
        if len(matches) != 1:
            raise HarnessError(f"无法发现所选 {role} 模型")
        contracts[role] = Contract(matches[0], openapi)
    return contracts
