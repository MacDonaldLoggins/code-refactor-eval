"""
LLM 客户端：封装 OpenAI 兼容 API 调用
"""
import os
import time
from openai import OpenAI
from dotenv import load_dotenv


def get_client():
    """获取 OpenAI 客户端"""
    load_dotenv()
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    if not base_url or not api_key:
        raise ValueError("缺少 OPENAI_BASE_URL 或 OPENAI_API_KEY 环境变量")
    return OpenAI(base_url=base_url, api_key=api_key)


def call_llm(prompt: str, model: str = None, temperature: float = 0.0,
             max_retries: int = 3, timeout: float = None) -> str:
    """
    调用 LLM 返回文本

    Args:
        prompt: 输入提示词
        model: 模型名称（默认使用 TEXT_MODEL）
        temperature: 温度参数
        max_retries: API 调用失败时的最大重试次数
        timeout: 单次请求超时（秒）。默认读环境变量 LLM_TIMEOUT，未设置则 600s。
                 生成 rubric 等大段代码耗时较长，故调大默认值避免 Request timed out。

    Returns:
        模型输出的文本
    """
    if model is None:
        load_dotenv()
        model = os.getenv("TEXT_MODEL", "gpt-5.5")

    if timeout is None:
        load_dotenv()
        timeout = float(os.getenv("LLM_TIMEOUT", "600"))

    client = get_client()

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"  → 正在请求 {model}（超时 {timeout:.0f}s）...", flush=True, end="")
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                timeout=timeout
            )
            print(" 完成", flush=True)
            content = response.choices[0].message.content
            if not content or not content.strip():
                raise ValueError("LLM 返回空内容")
            return content
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait = 2 ** attempt  # 指数退避: 2s, 4s, 8s...
                print(f"  ⚠️  LLM 调用失败 (第 {attempt}/{max_retries} 次): {e}，{wait}s 后重试...")
                time.sleep(wait)
            else:
                print(f"  ❌ LLM 调用失败，已重试 {max_retries} 次")

    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_error}")

