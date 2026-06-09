import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

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


# ---------- 远程加密同步 ----------

# 状态严重程度：数值越大越"差"，合并时取更差的
_STATUS_SEVERITY = {
    'valid': 0,
    'error': 1,
    'rate_limited': 2,
    'quota_exhausted': 3,
    'invalid': 4,
    'disabled': 5,
}

# 推送时剥离的本地字段
_LOCAL_ONLY_FIELDS = {'sync', 'local_key', 'port'}


def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    """PBKDF2 派生 Fernet 密钥"""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode('utf-8')))


def encrypt_config(json_str: str, password: str) -> str:
    """加密 JSON 字符串，返回 base64 编码的密文"""
    from cryptography.fernet import Fernet
    salt = os.urandom(16)
    key = _derive_fernet_key(password, salt)
    token = Fernet(key).encrypt(json_str.encode('utf-8'))
    return base64.b64encode(salt + token).decode('ascii')


def decrypt_config(encrypted_b64: str, password: str) -> str:
    """解密 base64 密文，返回 JSON 字符串。失败抛出异常"""
    from cryptography.fernet import Fernet
    raw = base64.b64decode(encrypted_b64)
    salt, token = raw[:16], raw[16:]
    key = _derive_fernet_key(password, salt)
    return Fernet(key).decrypt(token).decode('utf-8')


def _validate_remote_config(remote: dict) -> bool:
    """校验远程配置结构是否合法"""
    if not isinstance(remote, dict):
        return False
    apikeys = remote.get('apikeys')
    if not isinstance(apikeys, dict):
        return False
    endpoints = remote.get('endpoints')
    if not isinstance(endpoints, dict):
        return False
    # 至少有一个端点
    if not endpoints:
        return False
    return True


def merge_apikeys(local_apikeys: dict, remote_apikeys: dict) -> dict:
    """合并本地和远程 apikeys，保留本地更差的状态

    规则：
    - 远程有、本地无 → 添加（远程状态）
    - 远程无、本地有 → 保留（本地 key，可能是朋友自己加的或管理员刚删的）
    - 两边都有 → 取状态更差的那个
    """
    result = {}
    all_endpoints = set(local_apikeys.keys()) | set(remote_apikeys.keys())

    for ep in all_endpoints:
        local_keys = local_apikeys.get(ep, [])
        remote_keys = remote_apikeys.get(ep, [])

        # 本地 key → {key_value: entry}
        local_map = {}
        for k in local_keys:
            if isinstance(k, dict) and 'key' in k:
                local_map[k['key']] = k

        remote_map = {}
        for k in remote_keys:
            if isinstance(k, dict) and 'key' in k:
                remote_map[k['key']] = k

        merged = []
        all_key_vals = set(local_map.keys()) | set(remote_map.keys())

        for key_val in all_key_vals:
            local_entry = local_map.get(key_val)
            remote_entry = remote_map.get(key_val)

            if local_entry and remote_entry:
                # 两边都有，取更差的状态
                local_sev = _STATUS_SEVERITY.get(local_entry.get('status', 'valid'), 0)
                remote_sev = _STATUS_SEVERITY.get(remote_entry.get('status', 'valid'), 0)
                if local_sev >= remote_sev:
                    merged.append(local_entry)
                else:
                    merged.append(remote_entry)
            elif remote_entry:
                # 只在远程，添加
                merged.append(remote_entry)
            else:
                # 只在本地，保留
                merged.append(local_entry)

        if merged:
            result[ep] = merged

    return result


def sync_push(config: dict) -> bool:
    """加密 config 并推送到 GitHub Gist。剥离本地字段后推送。返回是否成功"""
    sync = config.get('sync', {})
    gist_id = sync.get('gist_id', '')
    password = sync.get('password', '')
    if not gist_id or not password:
        logger.warning("sync 配置不完整，跳过推送")
        return False

    # 剥离本地字段，只推送共享数据
    push_data = {k: v for k, v in config.items() if k not in _LOCAL_ONLY_FIELDS}

    try:
        encrypted = encrypt_config(json.dumps(push_data, ensure_ascii=False), password)
    except ImportError:
        logger.warning("cryptography 未安装，无法加密推送")
        return False
    except Exception as e:
        logger.error(f"加密失败: {e}")
        return False

    try:
        with tempfile.NamedTemporaryFile('w', suffix='.enc', delete=False, encoding='utf-8') as f:
            f.write(encrypted)
            tmp_path = f.name
        result = subprocess.run(
            ['gh', 'gist', 'edit', gist_id, tmp_path],
            capture_output=True, text=True, timeout=30,
        )
        Path(tmp_path).unlink(missing_ok=True)
        if result.returncode != 0:
            logger.error(f"Gist 推送失败: {result.stderr.strip()}")
            return False
        logger.info(f"配置已推送到 Gist {gist_id[:8]}...")
        return True
    except FileNotFoundError:
        logger.error("gh CLI 未安装，无法推送 Gist")
        return False
    except Exception as e:
        logger.error(f"Gist 推送异常: {e}")
        return False


def sync_pull(password_override: str | None = None) -> dict | None:
    """从远程 URL 拉取并解密配置，返回 config dict 或 None"""
    try:
        config = load_config()
    except Exception:
        return None
    sync = config.get('sync', {})
    url = sync.get('remote_url', '')
    password = password_override or sync.get('password', '')
    if not url or not password:
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'mimo-route'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            encrypted = resp.read().decode('ascii')
        decrypted = decrypt_config(encrypted, password)
        remote = json.loads(decrypted)
        if not _validate_remote_config(remote):
            logger.warning("远程配置结构校验失败，忽略")
            return None
        return remote
    except Exception as e:
        logger.warning(f"远程配置拉取失败: {e}")
        return None


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
