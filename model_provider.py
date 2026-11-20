"""Send one non-streaming Chat Completions request using the standard library."""

import http.client
import ipaddress
import json
import ssl
import time
from urllib.parse import urlsplit


_TIMEOUT_SECONDS = 45
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ModelProviderError(RuntimeError):
    """A provider error whose public message is safe to display."""


def _destination(endpoint):
    if not isinstance(endpoint, str) or not endpoint.isascii():
        raise ModelProviderError("请填写完整的 ASCII 模型接口地址。")
    if any(ord(char) <= 32 or ord(char) == 127 for char in endpoint):
        raise ModelProviderError("模型接口地址包含空白或控制字符。")
    if any(char in endpoint for char in ("?", "#", "\\")):
        raise ModelProviderError("模型接口地址不能包含查询参数、片段或反斜杠。")
    try:
        url = urlsplit(endpoint)
        host, port = url.hostname, url.port
    except ValueError:
        raise ModelProviderError("模型接口地址或端口格式有误。") from None
    if not host or "@" in url.netloc or "%" in url.netloc or port == 0:
        raise ModelProviderError("请填写有效的模型接口主机和端口，并移除地址中的凭据。")
    if url.scheme not in ("https", "http") or not url.path.rstrip("/").endswith("/chat/completions"):
        raise ModelProviderError("请填写以 /chat/completions 结尾的完整 HTTP(S) 接口地址。")
    if url.scheme == "http":
        if host == "localhost":
            # Keep plaintext connections local even with custom name resolution.
            host = "127.0.0.1"
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ModelProviderError("远程模型接口需要 HTTPS。HTTP 仅可用于本机回环地址。")
    return url.scheme, host, port, url.path


def ask_model(endpoint, model, api_key, messages) -> str:
    """Return the first assistant reply. An empty API key omits Authorization."""
    scheme, host, port, path = _destination(endpoint)
    if not isinstance(model, str) or not model.strip():
        raise ModelProviderError("请填写模型名称。")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise ModelProviderError("请提供非空的消息对象列表。")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key is not None and api_key != "":
        if not isinstance(api_key, str) or any(not 33 <= ord(c) <= 126 for c in api_key):
            raise ModelProviderError("API 密钥中含有无效字符。")
        headers["Authorization"] = "Bearer " + api_key
    try:
        payload = json.dumps(
            {"model": model.strip(), "messages": messages, "stream": False},
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise ModelProviderError("模型请求内容无法编码为 JSON。") from None

    if scheme == "https":
        connection = http.client.HTTPSConnection(
            host, port, timeout=_TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=_TIMEOUT_SECONDS)
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    try:
        connection.request("POST", path, body=payload, headers=headers)
        with connection.getresponse() as response:
            if 300 <= response.status < 400:
                raise ModelProviderError("模型接口返回重定向，请填写最终接口地址。")
            if not 200 <= response.status < 300:
                raise ModelProviderError(f"模型接口返回 HTTP {response.status}。")
            if response.length is not None and response.length > _MAX_RESPONSE_BYTES:
                raise ModelProviderError("模型回复超过大小限制。")
            data = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                chunk = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _MAX_RESPONSE_BYTES:
                    raise ModelProviderError("模型回复超过大小限制。")
            if response.length:
                raise ModelProviderError("模型接口提前关闭了连接，回复不完整。")
    except TimeoutError:
        raise ModelProviderError("模型请求超时。") from None
    except (OSError, http.client.HTTPException, ValueError, OverflowError):
        raise ModelProviderError("无法完成模型请求，请检查接口地址、网络及证书。") from None
    finally:
        connection.close()

    try:
        content = json.loads(data)["choices"][0]["message"]["content"]
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise ModelProviderError("模型接口未返回可识别的对话回复。") from None
    if not isinstance(content, str) or not content.strip():
        raise ModelProviderError("模型接口未返回文本回复。")
    return content
