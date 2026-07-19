"""
阿里云百炼 DashScope 图片生成协议适配插件
=============================================

通过 Monkey Patch 注入到 main.py 的 globals()，实现对百炼 DashScope 原生接口的支持。
main.py 只需在 if __name__ == "__main__" 块内加 2 行 import 即可，上游更新冲突极小。

支持的模型：
  - 千问 qwen-image 系列（同步）：qwen-image-2.0, qwen-image-2.0-pro, qwen-image-max 等
  - 万相 wan 系列（异步轮询）：wan2.7-image-pro, wan2.7-image 等

上游更新合并步骤：
  1. git fetch upstream && git merge upstream/main
  2. 如果 main.py 中 import 行有冲突，手动保留即可
  3. 如果 generate_ai_image 函数签名变更，在 _patched_generate_ai_image() 中调整
  4. 如果 test_provider_connection 路由路径变更，在 _patched_test_provider_connection() 中调整

架构说明：
  ─ plugins/dashscope_adapter.py  ← 本文件，全部逻辑
  ─ main.py                       ← 仅 2 行：sys.path + import（在 __main__ 块内）
  ─ static/api-settings.html      ← 协议下拉框新增 dashscope 选项
  ─ static/js/api-settings.js     ← API_PROTOCOLS + keepManualProtocol 新增 dashscope
"""

import json
import os
import re
import time
import asyncio
import logging
from datetime import datetime

import httpx
from fastapi import HTTPException

logger = logging.getLogger("dashscope_adapter")

# ─────────────────────────────────────────────────────
# 日志配置：失败信息写入独立文件，方便直接读取分析
# ─────────────────────────────────────────────────────

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_LOG_FILE = os.path.join(_LOG_DIR, "dashscope_errors.log")


def _log_error(action: str, error: str, detail: dict = None):
    """记录失败信息到 dashscope_errors.log。
    
    每行一条 JSON，格式：
      {"ts":"2026-07-18T23:16:00","action":"generate_image","error":"HTTP 502","detail":{...}}
    
    用 Python 读取示例：
      import json
      errors = [json.loads(line) for line in open('dashscope_errors.log') if line.strip()]
    """
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "error": str(error)[:500],
    }
    if detail:
        # 脱敏 api_key
        safe = {}
        for k, v in detail.items():
            if isinstance(k, str) and "key" in k.lower() and isinstance(v, str) and len(v) > 12:
                safe[k] = v[:8] + "..." + v[-6:]
            else:
                safe[k] = v
        entry["detail"] = safe
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志写入失败不应阻断主流程


# ─────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────

IMAGE_POLL_INTERVAL = float(os.getenv("IMAGE_POLL_INTERVAL", "2"))


def _clean_base(base_url):
    """智能清理 base_url 尾部路径，提取 host 根域名。
    百炼用户可能填写 /api/v1、/compatible-mode/v1 等后缀，需要统一砍掉再按需拼接。
    """
    base = str(base_url or "").strip().rstrip("/")
    for suffix in ("/compatible-mode/v1", "/api/v1", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def _size_from_pair(size):
    """将 1024x1024 转换为百炼格式的 1024*1024。"""
    try:
        parts = str(size or "").strip().lower().split("x")
        if len(parts) == 2:
            return f"{parts[0]}*{parts[1]}"
    except Exception:
        pass
    return str(size or "2048*2048")


def _get_api_key(provider):
    """从 provider 对象或环境变量中读取百炼 API Key。"""
    api_key = str((provider or {}).get("api_key") or "").strip()
    if api_key:
        return api_key
    provider_id = str((provider or {}).get("id") or "dashscope").strip().lower()
    env_key = f"API_PROVIDER_{re.sub(r'[^A-Za-z0-9]', '_', provider_id).upper()}_KEY"
    return os.getenv(env_key, "") or ""


def _is_provider(provider):
    """判断是否为百炼 DashScope provider。
    支持三种匹配方式，确保即使保存时 protocol 未正确写入也能识别：
    1. protocol 字段 == 'dashscope'
    2. id 字段 == 'dashscope'
    3. base_url 包含百炼特征域名（兜底）
    """
    protocol = str((provider or {}).get("protocol") or "").strip().lower()
    pid = str((provider or {}).get("id") or "").strip().lower()
    if protocol == "dashscope" or pid == "dashscope":
        return True
    base_url = str((provider or {}).get("base_url") or "").lower()
    if "maas.aliyuncs.com" in base_url or "dashscope.aliyuncs.com" in base_url:
        return True
    return False


def _build_reference_image_content(reference_images, g):
    """将参考图列表转为百炼 messages content 中的 image 条目。

    支持三种来源：
      1. 本地文件路径（/assets/xxx、/output/xxx）→ 转 base64 data URL
      2. 已经是 data:image/xxx;base64,... → 直接使用
      3. 远程 URL（https://...）→ 直接透传

    百炼 content 格式：[{"image": "base64或URL"}]
    """
    refs = reference_images or []
    if not refs:
        return []

    output_file_from_url = g.get("output_file_from_url")
    max_refs = g.get("ONLINE_IMAGE_REFERENCE_MAX", 20)
    items = []

    for ref in refs[:max_refs]:
        if not isinstance(ref, dict):
            continue
        url = str(ref.get("url") or "").strip()
        if not url:
            continue

        # 已经是 data URL，直接用
        if url.startswith("data:image/"):
            items.append({"image": url})
            continue

        # 本地文件：通过 main.py 的 output_file_from_url 找到路径 → 转 base64
        local_path = None
        if output_file_from_url:
            local_path = output_file_from_url(url)
        if local_path:
            try:
                with open(local_path, "rb") as f:
                    data = f.read()
                # 根据扩展名推断 MIME
                import mimetypes as _mt
                mime = _mt.guess_type(local_path)[0] or "image/png"
                encoded = __import__("base64").b64encode(data).decode("ascii")
                items.append({"image": f"data:{mime};base64,{encoded}"})
                continue
            except Exception as e:
                logger.warning(f"读取参考图本地文件失败: {local_path}, {e}")
                _log_error("ref_local_file", f"读取失败: {e}", {"path": str(local_path)})
                continue

        # 远程 URL，直接透传
        if url.startswith("http://") or url.startswith("https://"):
            items.append({"image": url})
            continue

    return items


def _extract_image_url(raw):
    """从百炼 DashScope 响应中手动提取图片 URL。
    百炼结构：{"output":{"choices":[{"message":{"content":[{"image":"url"}]}}]}}
    main.py 的 extract_image() 无法解析，因为 choices/message/content 不在其 IMAGE_CONTAINER_KEY_HINTS 中。
    """
    img_url = ""
    output = raw.get("output") or {}
    for choice in output.get("choices") or []:
        content = (choice.get("message") or {}).get("content") or []
        for item in content:
            if isinstance(item, dict) and item.get("image"):
                img_url = item["image"]
                break
        if img_url:
            break
    return img_url


# ─────────────────────────────────────────────────────
# 百炼 DashScope 生图函数
# ─────────────────────────────────────────────────────

async def _generate_dashscope_image(prompt, size, model, reference_images=None, provider=None, g=None):
    """通过百炼 DashScope 原生接口生成图片。
    千问 qwen-image → 同步调用 /api/v1/services/aigc/multimodal-generation/generation
    万相 wan       → 异步轮询 /api/v1/services/aigc/image-generation/generation
    """
    api_key = _get_api_key(provider)
    if not api_key:
        _log_error("generate_image", "未配置 API Key", {"provider_id": (provider or {}).get("id")})
        raise HTTPException(status_code=400, detail="未配置百炼 DashScope API Key，请在 API 设置中填写。")

    root = _clean_base((provider or {}).get("base_url") or "https://dashscope.aliyuncs.com")
    ds_size = _size_from_pair(size)
    messages = [{"role": "user", "content": [{"text": prompt.strip()}]}]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout_conf = httpx.Timeout(connect=20.0, read=1800.0, write=120.0, pool=20.0)
    is_wan = str(model or "").strip().lower().startswith("wan")

    if is_wan:
        # ── 万相异步模式：提交任务 → 轮询结果 ──
        # 参考图处理
        image_content = _build_reference_image_content(reference_images, g)
        if image_content:
            messages[0]["content"] = messages[0]["content"] + image_content
        gen_url = f"{root}/api/v1/services/aigc/image-generation/generation"
        async with httpx.AsyncClient(timeout=timeout_conf) as client:
            submit_res = await client.post(
                gen_url, headers={**headers, "X-DashScope-Async": "enable"},
                json={"model": model, "input": {"messages": messages}, "parameters": {"size": ds_size, "n": 1}},
            )
            submit_res.raise_for_status()
            raw = submit_res.json()
            task_id = (raw.get("output") or {}).get("task_id")
            if not task_id:
                img_url = _extract_image_url(raw)
                if img_url:
                    return {"type": "url", "value": img_url}, raw
                msg = f"百炼未返回 task_id：{json.dumps(raw, ensure_ascii=False)[:800]}"
                _log_error("generate_image_wan_submit", msg, {"model": model, "url": gen_url})
                raise HTTPException(status_code=502, detail=msg)
            deadline = time.monotonic() + 300
            last_payload = raw
            while time.monotonic() < deadline:
                await asyncio.sleep(IMAGE_POLL_INTERVAL)
                result = await client.get(f"{root}/api/v1/tasks/{task_id}", headers=headers)
                result.raise_for_status()
                data = result.json()
                last_payload = data
                task_output = data.get("output") or {}
                status = str(task_output.get("task_status") or "").upper()
                if status == "SUCCEEDED":
                    img_url = _extract_image_url(data)
                    if img_url:
                        return {"type": "url", "value": img_url}, data
                    msg = f"百炼成功但无图片：{json.dumps(data, ensure_ascii=False)[:800]}"
                    _log_error("generate_image_wan_poll", msg, {"model": model, "task_id": task_id})
                    raise HTTPException(status_code=502, detail=msg)
                if status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}:
                    msg = f"百炼任务失败：{data.get('message') or task_output.get('message')}"
                    _log_error("generate_image_wan_failed", msg, {"model": model, "task_id": task_id, "task_status": status})
                    raise HTTPException(status_code=502, detail=msg)
            msg = f"百炼生图超时(5min)：{json.dumps(last_payload, ensure_ascii=False)[:800]}"
            _log_error("generate_image_wan_timeout", msg, {"model": model, "task_id": task_id})
            raise HTTPException(status_code=504, detail=msg)
    else:
        # ── 千问同步模式 ──
        # 参考图处理：将 reference_images 转为 base64 插入 messages content
        image_content = _build_reference_image_content(reference_images, g)
        if image_content:
            messages[0]["content"] = messages[0]["content"] + image_content
        gen_url = f"{root}/api/v1/services/aigc/multimodal-generation/generation"
        async with httpx.AsyncClient(timeout=timeout_conf) as client:
            response = await client.post(gen_url, headers=headers,
                json={"model": model, "input": {"messages": messages}, "parameters": {"size": ds_size, "n": 1}})
            response.raise_for_status()
            raw = response.json()
            img_url = _extract_image_url(raw)
            if img_url:
                return {"type": "url", "value": img_url}, raw
            msg = f"百炼返回格式异常：{json.dumps(raw, ensure_ascii=False)[:800]}"
            _log_error("generate_image_qwen", msg, {"model": model, "url": gen_url})
            raise HTTPException(status_code=502, detail=msg)


# ─────────────────────────────────────────────────────
# Monkey Patch：注入到 main.py 的 globals() 中
# ─────────────────────────────────────────────────────

def apply_patches(g):
    """注入百炼 DashScope 适配。

    参数 g: main.py 的 globals() 字典。
    Python 所有函数共享同一个 __globals__，替换 globals dict 中的函数引用后，
    已定义的 build_online_image_result 等函数也能看到新函数。

    注入点：
      1. SUPPORTED_PROVIDER_PROTOCOLS — 注册协议名
      2. FIXED_PROTOCOL_PROVIDER_IDS — 固定协议不被自动覆盖
      3. is_dashscope_provider() — 判断函数
      4. generate_ai_image — wrap 函数，拦截 dashscope 生图
      5. test_provider_connection — wrap 函数，拦截 dashscope 验证
      6. fetch_models_from_upstream — wrap 函数，拦截 dashscope 模型拉取
    """

    # ── 1. 注册协议到常量集合 ──
    g["SUPPORTED_PROVIDER_PROTOCOLS"].add("dashscope")
    g["FIXED_PROTOCOL_PROVIDER_IDS"].add("dashscope")

    # ── 2. 注册判断函数 ──
    g["is_dashscope_provider"] = _is_provider

    # ── 3. Wrap generate_ai_image，拦截 dashscope provider ──
    _orig_gen = g["generate_ai_image"]

    async def _patched_gen(prompt, size, quality, model, reference_images=None, provider_id="comfly"):
        provider = g["get_api_provider"](provider_id)
        if _is_provider(provider):
            return await _generate_dashscope_image(prompt, size, model, reference_images, provider, g=g)
        return await _orig_gen(prompt, size, quality, model, reference_images, provider_id)

    g["generate_ai_image"] = _patched_gen

    # ── 3.5 Wrap resolve_chat_provider，拦截 dashscope 协议对话 ──
    # dashscope 的对话走 OpenAI 兼容路径 /compatible-mode/v1/chat/completions
    # main.py 的默认逻辑会把 base_url（如 .../api/v1）拼成 .../api/v1/v1，路径重复
    _orig_resolve_chat = g["resolve_chat_provider"]

    def _patched_resolve_chat(provider, model, ms_model):
        api_provider = g["get_api_provider"](provider or "")
        if not _is_provider(api_provider):
            return _orig_resolve_chat(provider, model, ms_model)
        base_root = (api_provider.get("base_url") or "").strip().rstrip("/")
        if not base_root:
            raise HTTPException(status_code=400, detail="百炼 DashScope 未配置 Base URL")
        # 清理尾部路径，统一用 OpenAI 兼容接口
        clean = _clean_base(base_root)
        base = clean + "/compatible-mode/v1"
        api_key = _get_api_key(api_provider)
        if not api_key:
            raise HTTPException(status_code=400, detail="未配置百炼 DashScope API Key")
        default_model = (api_provider.get("chat_models") or ["qwen3.7-plus"])[0]
        mdl = model or default_model
        hdrs = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        return base, hdrs, mdl

    g["resolve_chat_provider"] = _patched_resolve_chat

    # ── 4. Wrap test_provider_connection，拦截 dashscope 协议验证 ──
    _orig_test = g["test_provider_connection"]

    async def _patched_test(payload):
        protocol = str(getattr(payload, "protocol", "") or "").strip().lower()
        if protocol != "dashscope":
            return await _orig_test(payload)
        base_url = str(getattr(payload, "base_url", "") or "").strip().rstrip("/")
        ds_key = _get_api_key({"id": "dashscope", "api_key": getattr(payload, "api_key", None) or ""})
        if not ds_key:
            _log_error("test_connection", "未填写 API Key")
            raise HTTPException(status_code=400, detail="请先填写百炼 DashScope API Key")
        clean = _clean_base(base_url)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(clean + "/compatible-mode/v1/models", headers={"Authorization": f"Bearer {ds_key}"})
                if resp.status_code >= 400:
                    _log_error("test_connection", f"HTTP {resp.status_code}", {"base_url": base_url, "body": resp.text[:300]})
                    raise HTTPException(status_code=502, detail=f"百炼模型列表请求失败 (HTTP {resp.status_code})")
                data = resp.json()
                all_models = [m.get("id") for m in data.get("data") or [] if m.get("id")]
                image_kw = ("image", "wan", "z-image")
                image_models = [m for m in all_models if any(k in m.lower() for k in image_kw)]
                chat_models = [m for m in all_models if m.lower().startswith(("qwen", "deepseek", "glm", "kimi")) and m not in image_models]
                return {"ok": True, "status": 200, "message": "百炼 DashScope 可用", "model_count": len(all_models),
                    "image_models": image_models, "chat_models": chat_models, "video_models": [], "all": all_models, "protocol": "dashscope"}
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            _log_error("test_connection", f"连接失败: {e}", {"base_url": base_url})
            raise HTTPException(status_code=502, detail=f"百炼 DashScope 连接失败：{e}")

    g["test_provider_connection"] = _patched_test

    # ── 5. Wrap fetch_models_from_upstream，拦截 dashscope 协议拉取模型 ──
    _orig_fetch_models = g.get("fetch_models_from_upstream")

    async def _patched_fetch_models(base_url, api_key, protocol="openai", image_request_mode="openai"):
        if protocol != "dashscope":
            if _orig_fetch_models:
                return await _orig_fetch_models(base_url, api_key, protocol, image_request_mode)
            raise HTTPException(status_code=500, detail="fetch_models_from_upstream 未找到")
        clean = _clean_base(base_url)
        ds_key = api_key or ""
        if not ds_key:
            _log_error("fetch_models", "未填写 API Key")
            raise HTTPException(status_code=400, detail="请先填写百炼 DashScope API Key")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(clean + "/compatible-mode/v1/models", headers={"Authorization": f"Bearer {ds_key}", "Accept": "application/json"})
                if resp.status_code >= 400:
                    _log_error("fetch_models", f"HTTP {resp.status_code}", {"base_url": base_url, "body": resp.text[:300]})
                    raise HTTPException(status_code=502, detail=f"百炼模型列表请求失败 (HTTP {resp.status_code}): {resp.text[:300]}")
                _looks_like_html = g.get("looks_like_html_response")
                if _looks_like_html and _looks_like_html(resp.text):
                    _log_error("fetch_models", "返回 HTML 而非 JSON", {"base_url": base_url})
                    raise HTTPException(status_code=400, detail="百炼返回网页 HTML，请检查请求地址是否为 API Base URL")
                data = resp.json() if resp.text else {}
                all_models = [m.get("id") for m in data.get("data") or [] if m.get("id")]
                image_kw = ("image", "wan", "z-image")
                image_models = [m for m in all_models if any(k in m.lower() for k in image_kw)]
                chat_models = [m for m in all_models if m.lower().startswith(("qwen", "deepseek", "glm", "kimi")) and m not in image_models]
                return {
                    "ok": True, "protocol": "dashscope", "status": resp.status_code,
                    "message": f"百炼 DashScope 可用 · 找到 {len(all_models)} 个模型",
                    "model_count": len(all_models), "total": len(all_models),
                    "image_models": image_models, "chat_models": chat_models, "video_models": [],
                    "all": all_models,
                }
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            _log_error("fetch_models", f"连接失败: {e}", {"base_url": base_url})
            raise HTTPException(status_code=502, detail=f"百炼 DashScope 连接失败：{e}")

    g["fetch_models_from_upstream"] = _patched_fetch_models

    # ── 6. 替换 FastAPI probe-async 路由处理函数 ──
    # 注意：probe_async_endpoint 是 @app.post 路由 handler，FastAPI 内部持有直接引用，
    # 替换 globals 不够，需要直接替换路由表中的 endpoint。
    app = g["app"]
    _orig_probe = g.get("probe_async_endpoint")

    async def _patched_probe(payload):
        protocol = str(getattr(payload, "protocol", "") or "").strip().lower()
        if protocol != "dashscope":
            if _orig_probe:
                return await _orig_probe(payload)
            raise HTTPException(status_code=500, detail="probe_async_endpoint 未找到")
        base_url = str(getattr(payload, "base_url", "") or "").strip().rstrip("/")
        ds_key = _get_api_key({"id": "dashscope", "api_key": getattr(payload, "api_key", None) or ""})
        if not ds_key:
            _log_error("probe_async", "未填写 API Key")
            raise HTTPException(status_code=400, detail="请先填写百炼 DashScope API Key")
        clean = _clean_base(base_url)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(clean + "/compatible-mode/v1/models", headers={"Authorization": f"Bearer {ds_key}"})
                if resp.status_code >= 400:
                    _log_error("probe_async", f"HTTP {resp.status_code}", {"base_url": base_url, "body": resp.text[:300]})
                    raise HTTPException(status_code=502, detail=f"百炼模型列表请求失败 (HTTP {resp.status_code})")
                data = resp.json()
                all_models = [m.get("id") for m in data.get("data") or [] if m.get("id")]
                image_kw = ("image", "wan", "z-image")
                image_models = [m for m in all_models if any(k in m.lower() for k in image_kw)]
                chat_models = [m for m in all_models if m.lower().startswith(("qwen", "deepseek", "glm", "kimi")) and m not in image_models]
                return {
                    "ok": True, "protocol": "dashscope", "status_code": 200,
                    "message": f"百炼 DashScope 可用 · 找到 {len(all_models)} 个模型",
                    "model_count": len(all_models), "total": len(all_models),
                    "image_models": image_models, "chat_models": chat_models, "video_models": [],
                    "all": all_models,
                    "image_request_mode": "openai",
                }
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            _log_error("probe_async", f"连接失败: {e}", {"base_url": base_url})
            raise HTTPException(status_code=502, detail=f"百炼 DashScope 连接失败：{e}")

    # 替换路由表中的 endpoint
    for route in app.routes:
        if getattr(route, "path", "") == "/api/providers/probe-async":
            route.dependant.call = _patched_probe
            if hasattr(route, "endpoint"):
                route.endpoint = _patched_probe
            break
    g["probe_async_endpoint"] = _patched_probe

    logger.info("百炼 DashScope 适配插件加载完成")
