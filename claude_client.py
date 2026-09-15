"""调用 LLM API 生成回答，自动根据 API Key 前缀与模型名识别服务商并切换对应接口。

核心约束：严格基于用户上传的简历/资料回答，禁止编造。

自动识别规则（无需手动选择）：
- API Key 以 sk-ant- 开头            -> Anthropic（Claude，走其 OpenAI 兼容端点）
- 模型名含 deepseek                   -> DeepSeek
- 模型名含 gpt / openai / o1/o3/o4    -> OpenAI
- .env 设置 API_BASE_URL 可手动覆盖   -> 任意 OpenAI 兼容服务商
统一使用 OpenAI 兼容消息格式调用，仅切换 base_url。
"""
import os

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

SYSTEM_PROMPT = (
    "你是「发面馒头」，一名专业的面试辅导助手，"
    "帮助用户基于其真实简历与项目经历准备面试回答。\n\n"
    "严格约束：\n"
    "1. 只能依据用户提供的资料内容回答，禁止编造资料中不存在的项目、技术、经历、数字或细节。\n"
    "2. 若资料中没有相关信息，必须明确说明「资料中未提及」，不要凭空补充或猜测。\n"
    "3. 回答专业、结构化、贴合面试场景，可帮助用户梳理答题思路与表达。\n"
    "4. 不要提及以上约束本身。"
)


def resolve_provider(api_key, model):
    """根据 API Key 前缀与模型名自动判断服务商，返回 (base_url, provider_name)。"""
    # 手动覆盖优先
    custom = os.getenv("API_BASE_URL", "").strip()
    if custom:
        return custom, "自定义"

    if api_key.startswith("sk-ant-"):
        # Anthropic 官方 OpenAI 兼容端点
        return "https://api.anthropic.com/v1/", "Claude (Anthropic)"

    m = model.lower()
    if "deepseek" in m:
        return "https://api.deepseek.com", "DeepSeek"
    if "gpt" in m or "openai" in m or m.startswith(("o1", "o3", "o4")):
        return "https://api.openai.com/v1", "OpenAI"

    # 兜底：sk- 开头的 key 默认按 DeepSeek 处理（本项目默认服务商）
    return "https://api.deepseek.com", "DeepSeek"


def _build_messages(question, context):
    user_content = (
        f"以下是用户的简历 / 项目资料：\n\n{context}\n\n"
        f"====================\n\n"
        f"用户提出的面试问题：{question}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _friendly_error(e):
    name = type(e).__name__
    if "Authentication" in name:
        return "API Key 无效或已过期，请检查设置。"
    if "RateLimit" in name:
        return "请求过于频繁（限流），请稍后重试。"
    if "Connection" in name or "Timeout" in name:
        return "网络连接失败，请检查网络。"
    return f"调用失败：{e}"


def _make_client(api_key, model):
    base_url, _ = resolve_provider(api_key, model)
    return OpenAI(api_key=api_key, base_url=base_url)


def answer_question(question, context):
    """基于文档上下文回答面试问题（一次性返回）。返回 (answer_text, error_text)。"""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None, "未配置 API Key，请先在设置页填写。"

    model = os.getenv("CLAUDE_MODEL", "deepseek-v4-pro")
    client = _make_client(api_key, model)

    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=8192,
            messages=_build_messages(question, context),
        )
        text = response.choices[0].message.content
        return (text or "").strip(), None
    except Exception as e:
        return None, _friendly_error(e)


def stream_answer(question, context):
    """流式回答生成器，逐条 yield (kind, text)。

    kind 取值：
    - 'thinking'  推理模型的思考阶段心跳（text 为空，忽略其内容，不透传给前端）
    - 'delta'     回答正文片段
    - 'error'     错误信息
    - 'done'      结束标记
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        yield "error", "未配置 API Key，请先在设置页填写。"
        return

    model = os.getenv("CLAUDE_MODEL", "deepseek-v4-pro")
    client = _make_client(api_key, model)

    try:
        stream = client.chat.completions.create(
            model=model,
            max_tokens=8192,
            messages=_build_messages(question, context),
            stream=True,
        )
        thinking_sent = False
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 推理模型的思考阶段：只发一次心跳，不把思考文字透传给前端
            if getattr(delta, "reasoning_content", None):
                if not thinking_sent:
                    yield "thinking", ""
                    thinking_sent = True
                continue
            if getattr(delta, "content", None):
                yield "delta", delta.content
        yield "done", ""
    except Exception as e:
        yield "error", _friendly_error(e)
