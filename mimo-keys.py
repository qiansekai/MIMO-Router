import asyncio
import sys
import re
import argparse
import base64
from datetime import datetime
from aiohttp import ClientSession, ClientTimeout
from mimo_core import CONFIG_PATH, load_config, save_config, probe_key, code_to_status, update_key_status


def is_valid_key(s: str) -> bool:
    return s.startswith(('tp-', 'sk-'))


def is_plausible_base64(s: str) -> bool:
    if len(s) < 4 or len(s) % 4 != 0:
        return False
    return bool(re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', s))


def decode_base64(text: str) -> str:
    current = text.strip()
    while not is_valid_key(current):
        if not is_plausible_base64(current):
            break
        try:
            decoded = base64.b64decode(current).decode('utf-8')
            if decoded == current:
                break
            current = decoded
        except Exception:
            break
    return current


async def detect_endpoint(session: ClientSession, key: str, endpoints: dict) -> dict:
    """并行尝试所有端点，返回 {'endpoint': name, 'status': status, ...}"""

    async def try_endpoint(name, url):
        code, error_body = await probe_key(session, key, url)
        if code == 401:
            return None
        if code == 200:
            return {'endpoint': name, 'status': 'valid', 'error_code': 0, 'message': 'OK'}
        status = code_to_status(code, error_body) or 'error'
        return {'endpoint': name, 'status': status, 'error_code': code, 'message': error_body}

    results = await asyncio.gather(*(try_endpoint(n, u) for n, u in endpoints.items()))
    for r in results:
        if r is not None:
            return r
    return None


async def check_key(session: ClientSession, key_info: dict, endpoints: dict) -> dict:
    """检测单个 key 的状态"""
    key = key_info['key']
    endpoint_name = key_info.get('endpoint', 'unknown')
    endpoint_url = endpoints.get(endpoint_name)
    base = {'key': key, 'endpoint': endpoint_name}

    if not endpoint_url:
        return {**base, 'status': 'error', 'message': 'Endpoint not found'}

    code, error_body = await probe_key(session, key, endpoint_url)
    if code == 200:
        return {**base, 'status': 'valid', 'message': 'OK'}
    if code == -1:
        return {**base, 'status': 'timeout', 'error_code': 0, 'message': error_body}

    status = code_to_status(code, error_body) or 'error'
    return {**base, 'status': status, 'error_code': code, 'message': error_body}


async def _import_one(session: ClientSession, raw_key: str, config: dict, endpoints: dict, force_base64: bool) -> dict:
    key = raw_key
    if force_base64 or not key.startswith(('tp-', 'sk-')):
        decoded = decode_base64(key)
        if decoded != key:
            print(f"Base64解码: {raw_key[:20]}... -> {decoded[:16]}...")
            key = decoded

    for endpoint, keys in config.get('apikeys', {}).items():
        for k in keys:
            if isinstance(k, dict) and k['key'] == key:
                return {'key': key, 'status': 'dup', 'endpoint': endpoint}

    result = await detect_endpoint(session, key, endpoints)
    if not result:
        return {'key': key, 'status': 'fail'}

    ep = result['endpoint']
    if result['status'] == 'valid':
        return {'key': key, 'status': 'ok', 'endpoint': ep}
    return {'key': key, 'status': 'error', 'endpoint': ep,
            'error_code': result['error_code'], 'message': result['message']}


async def cmd_import(args):
    raw_keys = args.keys
    config = load_config()
    endpoints = config.get('endpoints', {})

    seen = set()
    unique_keys = [k for k in raw_keys if k not in seen and not seen.add(k)]

    print(f"批量导入 {len(unique_keys)} 个key...\n")

    async with ClientSession() as session:
        tasks = [_import_one(session, k, config, endpoints, args.base64) for k in unique_keys]
        results = await asyncio.gather(*tasks)

    ok_count = 0
    for r in results:
        key_short = r['key'][:16] + '...'
        if r['status'] == 'dup':
            print(f"  {key_short}  已存在 ({r['endpoint']})")
        elif r['status'] == 'fail':
            print(f"  {key_short}  无法检测端点")
        elif r['status'] == 'error':
            print(f"  {key_short}  {r['endpoint']}  {r.get('error_code','')} {r['message']}")
        elif r['status'] == 'ok':
            ep = r['endpoint']
            if ep not in config['apikeys']:
                config['apikeys'][ep] = []
            config['apikeys'][ep].append({'key': r['key'], 'status': 'valid'})
            print(f"  {key_short}  -> {ep}")
            ok_count += 1

    if ok_count > 0:
        save_config(config)
        print(f"\n导入成功 {ok_count} 个")


async def cmd_check(args):
    config = load_config()
    endpoints = config.get('endpoints', {})
    all_keys = []
    disabled_count = 0

    for endpoint, keys in config.get('apikeys', {}).items():
        for k in keys:
            if isinstance(k, dict):
                if k.get('status') == 'disabled':
                    disabled_count += 1
                    continue
                all_keys.append({**k, 'endpoint': endpoint})

    print(f"\n检测 {len(all_keys)} 个key（跳过 {disabled_count} 个disabled）...\n")

    async with ClientSession() as session:
        tasks = [check_key(session, k, endpoints) for k in all_keys]
        results = await asyncio.gather(*tasks)

    print(f"{'Key':<20} {'Endpoint':<6} {'Status':<15} {'Code':<6} {'Message'}")
    print("-" * 78)

    counts = {'valid': 0, 'invalid': 0, 'quota_exhausted': 0, 'rate_limited': 0, 'error': 0}
    icons = {'valid': '+', 'invalid': 'x', 'quota_exhausted': '$', 'rate_limited': '!', 'timeout': '~', 'error': 'x'}

    for r in results:
        icon = icons.get(r['status'], '?')
        print(f"{r['key'][:16]+'...':<20} {r['endpoint']:<6} {icon} {r['status']:<13} {r.get('error_code',''):<6} {r['message']}")
        counts[r['status']] = counts.get(r['status'], 0) + 1

    print(f"\n汇总: valid={counts['valid']} invalid={counts['invalid']} quota_exhausted={counts['quota_exhausted']} rate_limited={counts['rate_limited']} error={counts.get('error',0)+counts.get('timeout',0)}")

    if not args.dry_run:
        transient = ('rate_limited', 'timeout', 'error')
        for result in results:
            for endpoint, keys in config['apikeys'].items():
                for i, k in enumerate(keys):
                    if isinstance(k, dict) and k['key'] == result['key']:
                        old = k.get('status', '')
                        new = result['status']
                        if new in transient and old == 'valid':
                            continue
                        config['apikeys'][endpoint][i]['status'] = new
                        if new != 'valid':
                            config['apikeys'][endpoint][i]['error_code'] = result.get('error_code', 0)
                            config['apikeys'][endpoint][i]['error_message'] = result.get('message', '')
                        else:
                            config['apikeys'][endpoint][i].pop('error_code', None)
                            config['apikeys'][endpoint][i].pop('error_message', None)
        save_config(config)
        print("\n配置已更新")

    return [r for r in results if r['status'] == 'valid']


def cmd_archive(args):
    """将 invalid/quota_exhausted 的 key 移入归档"""
    config = load_config()
    archived = 0

    if 'archive' not in config:
        config['archive'] = []

    for endpoint in list(config.get('apikeys', {}).keys()):
        remaining = []
        for k in config['apikeys'][endpoint]:
            if isinstance(k, dict) and k.get('status') in ('invalid', 'quota_exhausted'):
                config['archive'].append({**k, 'endpoint': endpoint, 'archived_at': datetime.now().isoformat()})
                archived += 1
            else:
                remaining.append(k)
        config['apikeys'][endpoint] = remaining

    if archived > 0:
        save_config(config)
        print(f"归档 {archived} 个失效 key")
    else:
        print("无需归档")

    total = sum(len(v) for v in config.get('apikeys', {}).values())
    valid = sum(1 for v in config.get('apikeys', {}).values() for k in v if isinstance(k, dict) and k.get('status') == 'valid')
    archive_total = len(config.get('archive', []))
    print(f"剩余: {total} 个 key（{valid} 个有效），归档: {archive_total} 个")


def main():
    parser = argparse.ArgumentParser(description='MimoRoute Key 管理')
    sub = parser.add_subparsers(dest='command')

    p_import = sub.add_parser('import', help='导入key')
    p_import.add_argument('keys', nargs='+', help='apikey（支持多个，自动识别base64）')
    p_import.add_argument('--base64', '-b', action='store_true', help='强制base64解码')

    p_check = sub.add_parser('check', help='检测所有key')
    p_check.add_argument('--dry-run', '-n', action='store_true', help='仅显示，不更新配置')

    p_archive = sub.add_parser('archive', help='归档失效key')
    p_archive.add_argument('--check', '-c', action='store_true', help='归档前先检测所有key')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'import':
        asyncio.run(cmd_import(args))
    elif args.command == 'check':
        valid = asyncio.run(cmd_check(args))
        sys.exit(0 if valid else 1)
    elif args.command == 'archive':
        if args.check:
            asyncio.run(cmd_check(args))
        cmd_archive(args)


if __name__ == '__main__':
    main()
