import asyncio
import json
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout

CONFIG_PATH = Path(__file__).parent / 'config.json'

PROBE_MODEL = 'mimo-v2.5-pro'
PROBE_TIMEOUT = 5

# 默认模型回退链：失败时按顺序尝试下一个模型
DEFAULT_MODEL_FALLBACK = ['mimo-v2.5-pro', 'mimo-v2.5']


def load_config() -> dict:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_model_fallback(config: dict) -> list[str]:
    """获取模型回退链，从配置或默认值"""
    return config.get('model_fallback', DEFAULT_MODEL_FALLBACK)


def save_config(config: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


async def probe_key(session: ClientSession, key: str, endpoint_url: str) -> tuple[int, str]:
    """探测单个 key，返回 (status_code, error_body)。-1=连接失败"""
    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01',
    }
    body = json.dumps({
        'model': PROBE_MODEL,
        'max_tokens': 1,
        'messages': [{'role': 'user', 'content': 'hi'}],
    }).encode('utf-8')
    try:
        async with session.post(
            f'{endpoint_url}/anthropic/v1/messages',
            headers=headers, data=body,
            timeout=ClientTimeout(total=PROBE_TIMEOUT),
        ) as resp:
            if resp.status == 200:
                return 200, ''
            resp_body = await resp.read()
            return resp.status, resp_body.decode('utf-8', errors='ignore')[:200]
    except Exception as e:
        return -1, str(e)[:200]


def code_to_status(code: int, error_body: str) -> str | None:
    """状态码 → key 状态，None=无法判断

    429 区分两种情况：
    - "quota exhausted" → 额度耗尽，归档
    - "Too many requests" → 限流，临时错误，不归档
    """
    if code == 200:
        return 'valid'
    if code in (401, 403):
        return 'invalid'
    if code == 429:
        body_lower = error_body.lower()
        if 'quota' in body_lower:
            return 'quota_exhausted'
        return 'rate_limited'
    return None


def update_key_status(key: str, status: str, error_code: int = 0, error_message: str = ''):
    """更新 config.json 中 key 的状态，quota_exhausted 自动归档。返回 (changed, config)"""
    try:
        config = load_config()
        changed = False
        for ep, key_list in config.get('apikeys', {}).items():
            for k in key_list:
                if isinstance(k, dict) and k['key'] == key:
                    if k.get('status') == 'disabled':
                        continue
                    if k.get('status') != status:
                        k['status'] = status
                        if status == 'valid':
                            k.pop('error_code', None)
                            k.pop('error_message', None)
                        else:
                            k['error_code'] = error_code
                            k['error_message'] = error_message
                        changed = True

                        if status in ('invalid', 'quota_exhausted'):
                            key_list.remove(k)
                            archived = config.setdefault('archived', {}).setdefault(ep, [])
                            k['archived_at'] = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')
                            archived.append(k)
        if changed:
            save_config(config)
        return changed, config
    except Exception:
        return False, None
