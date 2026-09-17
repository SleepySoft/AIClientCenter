"""
AgentHarnessClients 手动测试脚本

直接运行本文件，对本机安装的 Codex CLI / Kimi CLI 做真实冒烟测试：
    1. 无状态模式（stateless）：单轮问答 + validate_response 校验
    2. 会话模式（session）：两轮调用，验证上下文记忆与缓存（codex 会显示 cached_input_tokens）
    3. 健康检查路径：is_health_check 不污染会话
    4. AIClientManager 集成：注册 -> 获取 -> 释放 -> 统计

用法：
    python -m AIClientCenter.TestAgentHarnessClients          # 全部测试
    python -m AIClientCenter.TestAgentHarnessClients codex    # 只测 codex
    python -m AIClientCenter.TestAgentHarnessClients kimi     # 只测 kimi
"""

import logging
import sys
import time

try:
    from .AgentHarnessClients import (
        CodexCLIClient, KimiCLIClient, MODE_SESSION, MODE_STATELESS,
    )
    from .AIClientManager import AIClientManager, CLIENT_PRIORITY_CONSUMABLES
except ImportError:
    from AgentHarnessClients import (
        CodexCLIClient, KimiCLIClient, MODE_SESSION, MODE_STATELESS,
    )
    from AIClientManager import AIClientManager, CLIENT_PRIORITY_CONSUMABLES

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('TestAgentHarness')

CODEWORD = 'BLUEBERRY_4271'


def check(condition: bool, label: str) -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    return condition


def test_stateless(client) -> bool:
    print(f'\n===== {client.name} / 无状态模式 =====')
    response = client.chat(messages=[
        {'role': 'system', 'content': '你是一个精确的问答机器人，只输出要求的文本。'},
        {'role': 'user', 'content': '请只回复: PING_PONG'},
    ], temperature=0)

    if not check('error' not in response, f'调用成功 (keys={list(response.keys())})'):
        print('  响应:', response)
        return False

    err = client.validate_response(response, expected_content='PING_PONG')
    content = response['choices'][0]['message']['content']
    check(err is None, f'内容校验: {content[:80]!r}')
    usage = response.get('usage', {})
    check(usage.get('total_tokens', 0) > 0, f'usage 统计: {usage}')
    return err is None


def test_session(client) -> bool:
    print(f'\n===== {client.name} / 会话模式 =====')
    r1 = client.chat(messages=[
        {'role': 'system', 'content': '你是一个记忆助手，记住用户告诉你的一切，回复尽量简短。'},
        {'role': 'user', 'content': f'请记住暗号 {CODEWORD}，只回复 STORED。'},
    ], temperature=0)
    if not check('error' not in r1, '第一轮调用成功'):
        print('  响应:', r1)
        return False
    print(f'  第一轮回复: {r1["choices"][0]["message"]["content"][:80]!r}, usage={r1.get("usage")}')

    time.sleep(1)

    r2 = client.chat(messages=[
        {'role': 'system', 'content': '你是一个记忆助手，记住用户告诉你的一切，回复尽量简短。'},
        {'role': 'user', 'content': f'请记住暗号 {CODEWORD}，只回复 STORED。'},
        {'role': 'assistant', 'content': 'STORED'},
        {'role': 'user', 'content': '暗号是什么？只回复暗号本身。'},
    ], temperature=0)
    if not check('error' not in r2, '第二轮调用成功（会话续接）'):
        print('  响应:', r2)
        return False

    content = r2['choices'][0]['message']['content']
    ok = check(CODEWORD in content, f'上下文记忆: {content[:80]!r}')
    usage = r2.get('usage', {})
    cached = usage.get('cached_input_tokens', 0)
    print(f'  第二轮 usage={usage}' + (f' (缓存命中 {cached} tokens)' if cached else ''))
    return ok


def test_health_check(client) -> bool:
    print(f'\n===== {client.name} / 健康检查路径 =====')
    response = client.chat(messages=[{'role': 'user', 'content': client.test_prompt}],
                           is_health_check=True)
    ok = 'error' not in response
    check(ok, '健康检查调用成功')
    if ok:
        content = response['choices'][0]['message']['content']
        check('OK' in content.upper(), f'健康检查内容: {content[:60]!r}')
    else:
        print('  响应:', response)
    return ok


def test_manager_integration(make_client) -> bool:
    print('\n===== AIClientManager 集成 =====')
    manager = AIClientManager(base_check_interval_sec=3600, first_check_delay_sec=9999)
    client = make_client()
    manager.register_client(client)

    acquired = manager.get_available_client('tester')
    if not check(acquired is not None, 'get_available_client 获取成功'):
        return False
    check(acquired.name == client.name, f'获取到目标客户端: {acquired.name}')

    response = acquired.chat(messages=[{'role': 'user', 'content': '只回复: MANAGER_OK'}])
    ok = 'error' not in response
    check(ok, '通过获取的客户端调用成功')
    if not ok:
        print('  响应:', response)

    manager.release_client(acquired)
    stats = manager.get_client_stats()
    stats_list = stats['clients']
    check(len(stats_list) == 1 and stats_list[0]['meta']['name'] == client.name,
          f'stats 正常 (status={stats_list[0]["state"]["status"]}, health={stats_list[0]["state"]["health_score"]})')
    manager.stop_monitoring()
    return ok


def run_for(cli: str) -> bool:
    results = []

    if cli == 'codex':
        make_stateless = lambda: CodexCLIClient(name='Codex Stateless', mode=MODE_STATELESS)
        make_session = lambda: CodexCLIClient(name='Codex Session', mode=MODE_SESSION)
    else:
        make_stateless = lambda: KimiCLIClient(name='Kimi Stateless', mode=MODE_STATELESS)
        make_session = lambda: KimiCLIClient(name='Kimi Session', mode=MODE_SESSION)

    client = make_stateless()
    results.append(('stateless', test_stateless(client)))
    results.append(('health', test_health_check(client)))

    session_client = make_session()
    results.append(('session', test_session(session_client)))

    results.append(('manager', test_manager_integration(make_stateless)))

    print(f'\n----- {cli} 汇总 -----')
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return all(ok for _, ok in results)


if __name__ == '__main__':
    targets = sys.argv[1:] or ['codex', 'kimi']
    all_ok = True
    for t in targets:
        try:
            all_ok = run_for(t) and all_ok
        except Exception:
            logger.exception(f'{t} 测试出现异常')
            all_ok = False
    print(f'\n===== 总体结果: {"ALL PASS" if all_ok else "SOME FAILED"} =====')
    sys.exit(0 if all_ok else 1)
