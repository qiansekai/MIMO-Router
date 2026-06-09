import asyncio
import json
import logging
import time
import warnings
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector
from mimo_core import CONFIG_PATH, load_config, save_config, probe_key, code_to_status, update_key_status, get_model_fallback

warnings.filterwarnings('ignore', message='Unclosed client session')
warnings.filterwarnings('ignore', message='Unclosed connector')

LOG_DIR = Path(__file__).parent
LOG_MAX_DAYS = 2

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_DIR / 'mimo-router.log', maxBytes=10*1024*1024, backupCount=3, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('aiohttp').setLevel(logging.WARNING)


def cleanup_old_logs():
    log_file = LOG_DIR / 'mimo-router.log'
    if not log_file.exists():
        return
    cutoff = datetime.now() - timedelta(days=LOG_MAX_DAYS)
    cutoff_str = cutoff.strftime('%Y-%m-%d')
    tmp_file = log_file.with_suffix('.tmp')
    dropped = 0
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as src, \
             open(tmp_file, 'w', encoding='utf-8') as dst:
            for line in src:
                if line.startswith('[') and len(line) > 11:
                    date_str = line[1:11]
                    if date_str < cutoff_str:
                        dropped += 1
                        continue
                dst.write(line)
        if dropped:
            tmp_file.replace(log_file)
            logger.info(f"已清理 {dropped} 条过期日志 (早于 {cutoff_str})")
        else:
            tmp_file.unlink(missing_ok=True)
    except Exception:
        tmp_file.unlink(missing_ok=True)


class MimoRoute:
    _REQ_SKIP = frozenset({'host', 'content-length', 'transfer-encoding', 'connection'})
    _RESP_SKIP = frozenset({'transfer-encoding', 'connection', 'content-encoding'})

    def __init__(self):
        self.config = load_config()
        self.last_modified = 0
        self._session: ClientSession | None = None
        self._key_index = 0
        self._last_error_permanent = False
        self._last_cn_valid = -1
        self._last_sgp_valid = -1
        self._cached_keys: tuple[list[dict], list[dict]] | None = None
        self._config_check_time = 0.0
        self._config_check_interval = 2.0
        self._endpoint_down: dict[str, float] = {}  # endpoint_url -> last 404 time
        self._endpoint_cooldown = 10.0  # 404 后冷却秒数
        self._endpoint_404_count: dict[str, int] = {}  # endpoint_url -> 404 次数
        self._archived_refresh_interval = 600.0  # 归档 key 刷新间隔（10 分钟）
        self._max_retries = 3  # 最大重试次数

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            connector = TCPConnector(limit=100, ttl_dns_cache=300, keepalive_timeout=30)
            self._session = ClientSession(connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _load_config(self) -> dict:
        try:
            config = load_config()
            cn_total = len(config['apikeys'].get('cn', []))
            sgp_total = len(config['apikeys'].get('sgp', []))
            _unusable = ('invalid', 'disabled', 'quota_exhausted', 'rate_limited', 'error')
            cn_valid = sum(1 for k in config['apikeys'].get('cn', []) if isinstance(k, dict) and k.get('status') not in _unusable)
            sgp_valid = sum(1 for k in config['apikeys'].get('sgp', []) if isinstance(k, dict) and k.get('status') not in _unusable)
            logger.info(f"配置加载成功，cn: {cn_valid}/{cn_total}可用，sgp: {sgp_valid}/{sgp_total}可用，共 {cn_valid+sgp_valid} 个可用")
            return config
        except Exception as e:
            logger.error(f"配置加载失败: {e}")
            return {"apikeys": {"cn": [], "sgp": []}, "local_key": "123", "port": 18888, "endpoints": {"cn": "", "sgp": ""}}

    def _invalidate_config_cache(self):
        self._cached_keys = None
        self._config_check_time = 0

    async def _check_config_update(self):
        now = time.monotonic()
        if now - self._config_check_time < self._config_check_interval:
            return
        self._config_check_time = now
        try:
            loop = asyncio.get_running_loop()
            mtime = await loop.run_in_executor(None, lambda: CONFIG_PATH.stat().st_mtime)
            if mtime > self.last_modified:
                self.config = self._load_config()
                self.last_modified = mtime
                self._key_index = 0
                self._cached_keys = None
                logger.info("配置已热更新")
        except Exception as e:
            logger.warning(f"检查配置更新失败: {e}")

    def _get_all_keys(self) -> tuple[list[dict], list[dict]]:
        """返回 (valid_keys, recovery_keys)，valid 优先，带缓存"""
        if self._cached_keys is not None:
            return self._cached_keys
        valid, recovery = [], []
        for endpoint, key_list in self.config['apikeys'].items():
            for k in key_list:
                if not isinstance(k, dict):
                    continue
                entry = {'endpoint': endpoint, 'key': k['key'], 'status': k.get('status', 'valid')}
                status = k.get('status', 'valid')
                if status in ('invalid', 'quota_exhausted', 'rate_limited'):
                    recovery.append(entry)
                elif status not in ('disabled', 'error'):
                    valid.append(entry)
        self._cached_keys = (valid, recovery)
        return valid, recovery

    def _invalidate_key(self, key: str, endpoint: str, error_code: int, error_message: str, status: str = 'invalid'):
        """标记 key 失效，线程池执行"""
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._invalidate_key_sync, key, endpoint, error_code, error_message, status)

    def _invalidate_key_sync(self, key: str, endpoint: str, error_code: int, error_message: str, status: str = 'invalid'):
        changed, config = update_key_status(key, status, error_code, error_message)
        if changed and config:
            logger.warning(f"key {key[:16]}... 已失效 (endpoint: {endpoint}, code: {error_code})")
            self.config = config
            self._invalidate_config_cache()

    def _recover_key(self, key: str):
        """恢复 key 为 valid，线程池执行"""
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._recover_key_sync, key)

    def _recover_key_sync(self, key: str):
        changed, config = update_key_status(key, 'valid')
        if changed and config:
            logger.info(f"key {key[:16]}... 已恢复")
            self.config = config
            self._invalidate_config_cache()

    def _has_image_content(self, data: dict) -> bool:
        """检测请求是否包含图片/多模态内容"""
        messages = data.get('messages', [])
        for msg in messages:
            content = msg.get('content')
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get('type', '')
                        if item_type in ('image_url', 'image', 'file'):
                            return True
                        # 检测 base64 图片数据
                        if item_type == 'text':
                            text = item.get('text', '')
                            if 'data:image/' in text and ';base64,' in text:
                                return True
            elif isinstance(content, str):
                # 检测 base64 图片数据
                if 'data:image/' in content and ';base64,' in content:
                    return True
        return False

    async def _forward(self, session: ClientSession, request: web.Request, body: bytes, key: str, endpoint_url: str):
        """转发请求，返回 StreamResponse=成功，None=需要重试"""
        headers = {k: v for k, v in request.headers.items() if k.lower() not in self._REQ_SKIP}
        headers['Authorization'] = f'Bearer {key}'
        path = request.path
        if path.startswith('/v1/models'):
            target_url = f'{endpoint_url}{path}'
        elif not path.startswith('/anthropic'):
            target_url = f'{endpoint_url}/anthropic{path}'
        else:
            target_url = f'{endpoint_url}{path}'

        try:
            start = time.time()
            async with session.request(
                method=request.method, url=target_url, headers=headers,
                data=body, timeout=ClientTimeout(total=300)
            ) as resp:
                api_ver = headers.get('anthropic-version', 'none')

                if resp.status != 200:
                    resp_body = await resp.read()
                    latency = int((time.time() - start) * 1000)
                    error_body = resp_body.decode('utf-8', errors='ignore')[:200]
                    logger.info(f"key={key[:8]}... endpoint={endpoint_url} status={resp.status} latency={latency}ms error={error_body} anthropic-version={api_ver}")

                    if resp.status in (401, 402, 403, 429):
                        status = code_to_status(resp.status, error_body) or 'invalid'
                        self._invalidate_key(key, endpoint_url, resp.status, error_body, status)
                        self._last_error_permanent = (status != 'rate_limited')
                    else:
                        self._last_error_permanent = False
                        if resp.status == 404:
                            self._endpoint_down[endpoint_url] = time.monotonic()
                    return None

                resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in self._RESP_SKIP}
                resp_headers['Access-Control-Allow-Origin'] = '*'
                stream = web.StreamResponse(status=resp.status, headers=resp_headers)
                await stream.prepare(request)

                first_chunk = True
                async for chunk in resp.content.iter_any():
                    if first_chunk:
                        latency = int((time.time() - start) * 1000)
                        logger.info(f"key={key[:8]}... endpoint={endpoint_url} status=200 latency={latency}ms anthropic-version={api_ver}")
                        self._endpoint_down.pop(endpoint_url, None)
                        first_chunk = False
                    await stream.write(chunk)

                await stream.write_eof()
                return stream
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            logger.debug(f"客户端断开连接: {key[:8]}... endpoint={endpoint_url}")
            self._last_error_permanent = False
            return None
        except Exception as e:
            if 'Cannot write to closing transport' in str(e):
                logger.debug(f"客户端断开连接: {key[:8]}... endpoint={endpoint_url}")
            else:
                logger.error(f"转发失败: {e}")
            self._last_error_permanent = False
            return None

    async def _refresh_keys(self):
        """后台探测所有 key 状态，并行执行"""
        endpoints = self.config.get('endpoints', {})
        keys_snapshot = [
            (ep, k['key'])
            for ep, key_list in self.config.get('apikeys', {}).items()
            if endpoints.get(ep)
            for k in key_list
            if isinstance(k, dict) and k.get('status') != 'disabled'
        ]
        if not keys_snapshot:
            return

        session = await self._get_session()

        async def probe(ep, key):
            code, error_body = await probe_key(session, key, endpoints[ep])
            return ep, key, code, error_body

        results = await asyncio.gather(*(probe(ep, key) for ep, key in keys_snapshot))
        any_changed = False
        last_config = None
        for ep, key, code, error_body in results:
            status = code_to_status(code, error_body)
            if status is not None:
                changed, config = update_key_status(key, status, code if code > 0 else 0, error_body)
                if changed:
                    any_changed = True
                    last_config = config
                    logger.info(f"key {key[:16]}... 状态更新为 {status} (endpoint: {ep})")

        if any_changed and last_config:
            self.config = last_config
            self._invalidate_config_cache()
        cn_valid = sum(1 for k in self.config['apikeys'].get('cn', []) if isinstance(k, dict) and k.get('status') == 'valid')
        sgp_valid = sum(1 for k in self.config['apikeys'].get('sgp', []) if isinstance(k, dict) and k.get('status') == 'valid')
        if cn_valid != self._last_cn_valid or sgp_valid != self._last_sgp_valid:
            logger.info(f"key 刷新完成，cn: {cn_valid} 可用，sgp: {sgp_valid} 可用")
            self._last_cn_valid = cn_valid
            self._last_sgp_valid = sgp_valid

    async def _refresh_archived(self):
        """探测归档 key，恢复可用的到活跃列表"""
        config = load_config()
        endpoints = config.get('endpoints', {})

        # 已在活跃列表的 key
        active_keys = set()
        for key_list in config.get('apikeys', {}).values():
            for k in key_list:
                if isinstance(k, dict):
                    active_keys.add(k['key'])

        # 收集归档 key
        to_probe = []  # (key, endpoint)
        for k in config.get('archive', []):
            ep = k.get('endpoint', 'cn')
            if k['key'] not in active_keys:
                to_probe.append((k['key'], ep))
        for ep in ('cn', 'sgp'):
            for k in config.get('archived', {}).get(ep, []):
                if k['key'] not in active_keys:
                    to_probe.append((k['key'], ep))

        if not to_probe:
            return

        # 去重
        seen = set()
        unique = []
        for key, ep in to_probe:
            if key not in seen:
                seen.add(key)
                unique.append((key, ep))

        session = await self._get_session()

        async def probe(key, ep):
            url = endpoints.get(ep)
            if not url:
                return key, ep, -1, ''
            code, body = await probe_key(session, key, url)
            return key, ep, code, body

        results = await asyncio.gather(*(probe(k, e) for k, e in unique))
        recovered = {k: e for k, e, c, _ in results if c == 200}

        if not recovered:
            return

        # 重新加载配置，执行迁移
        config = load_config()
        active_keys = set()
        for key_list in config.get('apikeys', {}).values():
            for k in key_list:
                if isinstance(k, dict):
                    active_keys.add(k['key'])

        moved = 0
        # 从 archive 移出
        new_archive = []
        for k in config.get('archive', []):
            if k['key'] in recovered and k['key'] not in active_keys:
                ep = recovered[k['key']]
                k['status'] = 'valid'
                k.pop('error_code', None)
                k.pop('error_message', None)
                k.pop('archived_at', None)
                config.setdefault('apikeys', {}).setdefault(ep, []).append(k)
                active_keys.add(k['key'])
                moved += 1
            else:
                new_archive.append(k)
        config['archive'] = new_archive

        # 从 archived.cn/sgp 移出
        for ep in ('cn', 'sgp'):
            new_ep = []
            for k in config.get('archived', {}).get(ep, []):
                if k['key'] in recovered and k['key'] not in active_keys:
                    k['status'] = 'valid'
                    k.pop('error_code', None)
                    k.pop('error_message', None)
                    k.pop('archived_at', None)
                    config.setdefault('apikeys', {}).setdefault(ep, []).append(k)
                    active_keys.add(k['key'])
                    moved += 1
                else:
                    new_ep.append(k)
            config['archived'][ep] = new_ep

        if moved > 0:
            save_config(config)
            self.config = config
            self._invalidate_config_cache()
            logger.info(f"归档刷新: 恢复 {moved} 个 key 到活跃列表")

    async def _background_refresh(self, app: web.Application):
        """启动时立即刷新，之后每 30 秒轮询活跃 key，每 10 分钟轮询归档 key"""
        await self._refresh_keys()
        last_archived_refresh = 0.0
        while True:
            await asyncio.sleep(30)
            try:
                await self._refresh_keys()
            except Exception as e:
                logger.error(f"后台刷新异常: {e}")

            now = time.monotonic()
            if now - last_archived_refresh >= self._archived_refresh_interval:
                last_archived_refresh = now
                try:
                    await self._refresh_archived()
                except Exception as e:
                    logger.error(f"归档刷新异常: {e}")

    async def handle_request(self, request: web.Request):
        if request.method == 'OPTIONS':
            return web.Response(
                status=204,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
                    'Access-Control-Allow-Headers': '*',
                    'Access-Control-Max-Age': '86400',
                }
            )
        session = await self._get_session()
        body = await request.read()
        await self._check_config_update()

        # 解析请求体，处理模型转换
        data = None
        original_model = ''
        if request.content_type == 'application/json' and body:
            try:
                data = json.loads(body)
                original_model = data.get('model', '')
                modified = False

                if original_model.endswith('-nothinking'):
                    data['model'] = original_model.replace('-nothinking', '')
                    data['thinking'] = {'type': 'disabled'}
                    modified = True

                # 多模态检测：图片内容路由到 mimo-v2.5
                if self._has_image_content(data):
                    data['model'] = 'mimo-v2.5'
                    modified = True
                    logger.info(f"检测到图片内容，路由到 mimo-v2.5 (原模型: {original_model})")

                if modified:
                    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            except (json.JSONDecodeError, AttributeError):
                pass

        # 获取模型回退链
        fallback_chain = get_model_fallback(self.config)
        current_model_idx = 0
        if data and data.get('model'):
            current_model = data['model']
            if current_model in fallback_chain:
                current_model_idx = fallback_chain.index(current_model)

        valid_keys, recovery_keys = self._get_all_keys()
        if not valid_keys and not recovery_keys:
            # 无可用 key 时强制重新加载配置（可能刚导入了新 key）
            self.config = self._load_config()
            self._key_index = 0
            self._cached_keys = None
            valid_keys, recovery_keys = self._get_all_keys()
            if not valid_keys and not recovery_keys:
                logger.error("所有key不可用")
                return web.Response(status=503, text=json.dumps({'error': 'All keys exhausted'}), content_type='application/json',
                                    headers={'Access-Control-Allow-Origin': '*'})

        n = len(valid_keys)
        model_fallback_used = False

        # 模型回退循环
        for model_idx in range(current_model_idx, len(fallback_chain)):
            current_model = fallback_chain[model_idx]

            # 如果不是第一个模型，更新 body
            if model_idx > current_model_idx and data:
                data['model'] = current_model
                body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                model_fallback_used = True
                logger.info(f"模型回退: {fallback_chain[model_idx - 1]} -> {current_model}")

            retry_count = 0
            while retry_count < self._max_retries:
                now = time.monotonic()
                i = 0
                tried_any = False

                while i < n:
                    idx = (self._key_index + i) % n
                    info = valid_keys[idx]
                    endpoint_url = self.config['endpoints'].get(info['endpoint'])
                    if not endpoint_url:
                        i += 1
                        continue

                    # 检查端点冷却状态
                    down_since = self._endpoint_down.get(endpoint_url)
                    if down_since and now - down_since < self._endpoint_cooldown:
                        i += 1
                        continue

                    tried_any = True
                    resp = await self._forward(session, request, body, info['key'], endpoint_url)
                    if resp is not None:
                        self._key_index = (idx + 1) % n
                        if model_fallback_used:
                            logger.info(f"模型回退成功，使用: {current_model}")
                        return resp

                    if self._last_error_permanent:
                        i += 1
                        continue

                    # 404 等非永久错误：记录并尝试下一个端点
                    failed_endpoint = info['endpoint']
                    failed_endpoint_url = endpoint_url
                    self._endpoint_404_count[failed_endpoint_url] = self._endpoint_404_count.get(failed_endpoint_url, 0) + 1
                    logger.debug(f"端点 {failed_endpoint_url} 返回 404，累计: {self._endpoint_404_count[failed_endpoint_url]} 次")

                    # 跳过同端点的剩余 key
                    skipped = True
                    for j in range(i + 1, n):
                        next_idx = (self._key_index + j) % n
                        if valid_keys[next_idx]['endpoint'] != failed_endpoint:
                            i = j
                            skipped = False
                            break
                    if skipped:
                        break

                # 如果没有尝试任何 key（都被冷却），等待后重试
                if not tried_any:
                    min_wait = min(
                        (self._endpoint_cooldown - (now - t)
                         for t in self._endpoint_down.values() if now - t < self._endpoint_cooldown),
                        default=self._endpoint_cooldown
                    )
                    logger.info(f"所有端点冷却中，等待 {min_wait:.1f}s 后重试 ({retry_count + 1}/{self._max_retries})")
                    await asyncio.sleep(min_wait)
                    retry_count += 1
                    continue

                # 尝试了 key 但都失败，等待后重试
                retry_count += 1
                if retry_count < self._max_retries:
                    wait_time = min(2.0 * retry_count, 10.0)  # 递增等待，最多 10 秒
                    logger.info(f"本轮所有 key 失败，等待 {wait_time}s 后重试 ({retry_count}/{self._max_retries})")
                    await asyncio.sleep(wait_time)

        # 所有模型和重试都失败，尝试 recovery keys
        for info in recovery_keys:
            endpoint_url = self.config['endpoints'].get(info['endpoint'])
            if not endpoint_url:
                continue
            resp = await self._forward(session, request, body, info['key'], endpoint_url)
            if resp is not None:
                self._recover_key(info['key'])
                return resp

        logger.error("所有key不可用")
        return web.Response(status=503, text=json.dumps({'error': 'All keys exhausted'}), content_type='application/json',
                            headers={'Access-Control-Allow-Origin': '*'})


def create_app():
    route = MimoRoute()
    app = web.Application()

    async def on_startup(app):
        app['refresh_task'] = asyncio.create_task(route._background_refresh(app))

    async def on_cleanup(app):
        task = app.get('refresh_task')
        if task:
            task.cancel()
        await route.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route('*', '/{path:.*}', route.handle_request)
    return app


if __name__ == '__main__':
    cleanup_old_logs()
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            port = json.load(f).get('port', 18888)
    except Exception:
        port = 18888
    logger.info(f"启动 MimoRoute，端口: {port}")
    web.run_app(create_app(), host='0.0.0.0', port=port)
