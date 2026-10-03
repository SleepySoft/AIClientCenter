# --------------------------------------------------------------------------------
# Python is config. We don't need a json file and load it, analyze it.
# Rename ai_client_config_example.py to ai_client_config.py to enable this config.
# --------------------------------------------------------------------------------
import os
from typing import List, Dict

from GlobalConfig import *
from AIClientCenter.ZhipuSDKAdapter import ZhipuSDKAdapter
from AIClientCenter.AIClients import StandardOpenAIClient, \
    SelfRotatingOpenAIClient, OuterTokenRotatingOpenAIClient
from AIClientCenter.AIClientManager import CLIENT_PRIORITY_EXPENSIVE, \
    CLIENT_PRIORITY_FREEBIE, BaseAIClient, CLIENT_PRIORITY_NORMAL, CLIENT_PRIORITY_CONSUMABLES
from AIClientCenter.OpenAICompatibleAPI import create_siliconflow_client, create_modelscope_client, \
    create_long_cat_client
from AIClientCenter.AIServiceTokenRotator import SiliconFlowServiceRotator
from AIClientCenter.GoogleGeminiAdapter import GoogleGeminiAdapter
from AIClientCenter.OpenClawClient import OpenClawClient
from AIClientCenter.BudgetPolicy import BudgetMode, BudgetPolicy


def build_ai_clients() -> Dict[str, BaseAIClient]:
    # -------- The default silicon flow client --------
    # - Use high balance account's token
    # - It is considered available by default.
    # - Once lower value token is available, client manage will not suggest this client.
    # - The Initialize is in environment variant "SILICON_API_KEY", or set token here.
    # -------------------------------------------------

    sf_api_default = create_siliconflow_client('A valid token')
    sf_client_default = StandardOpenAIClient(
        name='SiliconFlow Client Default',
        openai_api=sf_api_default,
        priority=CLIENT_PRIORITY_EXPENSIVE,
        group_id='silicon flow',
        default_available=True,
        balance_config={ 'hard_threshold': 10 }
    )
    # Because there's no rotator to update its balance. Just set a good value to make it health.
    sf_client_default.update_balance(100)

    # -------- The token-rotation silicon flow client --------
    # - Use low-value token list.
    # - Init multiple clients, because sf will respond 504 when switching to another key.
    # - Note that there may be session limitation per ip, so we should put them in a same group and set group limit.
    # - Initialize token set to empty.
    # --------------------------------------------------------

    sf_api_a = create_siliconflow_client('invalid')
    sf_client_a = OuterTokenRotatingOpenAIClient(
        name='SiliconFlow Client A',
        openai_api=sf_api_a,
        priority=CLIENT_PRIORITY_NORMAL,
        group_id='silicon flow',
        balance_config={ 'hard_threshold': 0.1 }
    )
    sf_rotator_a = SiliconFlowServiceRotator(
        ai_client=sf_client_a,
        keys_file=os.path.join(CONFIG_PATH, 'sf_keys_a.txt'),
        keys_record_file=os.path.join(DATA_PATH, 'sf_keys_record_a.json'),
        threshold=0.1
    )

    # --------------------------------------------------------

    sf_api_b = create_siliconflow_client('invalid')
    sf_client_b = OuterTokenRotatingOpenAIClient(
        name='SiliconFlow Client B',
        openai_api=sf_api_b,
        priority=CLIENT_PRIORITY_NORMAL,
        group_id='silicon flow',
        balance_config={ 'hard_threshold': 0.1 }
    )
    sf_rotator_b = SiliconFlowServiceRotator(
        ai_client=sf_client_b,
        keys_file=os.path.join(CONFIG_PATH, 'sf_keys_b.txt'),
        keys_record_file=os.path.join(DATA_PATH, 'sf_keys_record_b.json'),
        threshold=0.1
    )

    # -- Start token rotator --

    # sf_rotator_a.run_in_thread()
    # sf_rotator_b.run_in_thread()

    # -------------- Longcat client --------------
    # - Daily refresh invoking times limit.
    # - Use this client by priority.
    # ------------------------------------------------

    longcat_api = create_long_cat_client('A valid token')
    longcat_client = OuterTokenRotatingOpenAIClient(
        name='Longcat Client',
        openai_api=longcat_api,
        priority=CLIENT_PRIORITY_NORMAL,
        group_id='longcat',
        balance_config={ 'hard_threshold': 0.1 }
    )
    # You can set the token limit but I just let it always seem healthy.
    longcat_client.update_balance(10)

    # -------------- Model scope client --------------
    # - Daily refresh invoking times limit.
    # - Use this client by priority.
    # ------------------------------------------------

    # Modelscope: A total of 2000 free API-Inference calls per day, with a limit of 500 calls per single model
    #             However, only the following three 400B+ models are actually available.
    #             There should be a more strict limitation. So just rotate the models and keys. Do not build too many clients.

    ms_models = [
        'deepseek-ai/DeepSeek-R1-0528',
        'deepseek-ai/DeepSeek-V3.2-Exp',
        'Qwen/Qwen3-Coder-480B-A35B-Instruct'
    ]

    ms_tokens = [           # Put your valid tokens here.
        'Token1',
        'Token2',
        'Token3'
    ]

    ms_api = create_modelscope_client(
        token="If you don't need token rotation, set the token here."
    )
    ms_client = SelfRotatingOpenAIClient(
        name=f'ModelScope Client',
        openai_api=ms_api,
        priority=CLIENT_PRIORITY_FREEBIE,
        group_id='model scope',
        default_available=True
    )
    ms_client.set_rotation_models(ms_models, rotate_per_times=5)
    ms_client.set_rotation_tokens(ms_tokens, rotate_per_times=15)   # Models (3) x Rotate Times (5) => Rotate Token (15)
    ms_client.set_usage_constraints(max_tokens=495, period_days=1, target_metric='request_count')

    # ----------------- Zhipu client -----------------
    # - A certain amount of credit will be given after
    #   registration and real-name authentication.
    # ------------------------------------------------

    zhipu_adapter = ZhipuSDKAdapter(
        api_key=os.getenv('ZHIPU_API_KEY', "your-zhipu-key"),
        enable_thinking=False
    )
    zhipu_client = StandardOpenAIClient(
        name="zhipu-01",
        openai_api=zhipu_adapter,
        priority=CLIENT_PRIORITY_CONSUMABLES,
        group_id='zhipu',
        default_available=True
    )

    # --------------------------------------------------------

    gemini_api_1 = GoogleGeminiAdapter(
        api_key='AIzaSyDQFb29QyRMTBpPAhoaLED23vu-mG0gb-k',
        model='gemini-2.5-flash',
        proxy='http://127.0.0.1:10809'
    )
    gemini_client_1 = StandardOpenAIClient(
        name="Gemini 1",
        openai_api=gemini_api_1,
        priority=CLIENT_PRIORITY_CONSUMABLES,
        group_id='gemini',
        default_available=True
    )

    # -------------- OpenClaw client --------------
    # - Communicates with OpenClaw agents via CLI
    # - Useful for routing requests through OpenClaw's agent system
    # - Supports timeout and error handling
    # ------------------------------------------------

    # openclaw_client = OpenClawClient(
    #     name='OpenClaw Asuka',
    #     agent_id='asuka',
    #     priority=CLIENT_PRIORITY_NORMAL,
    #     group_id='openclaw',
    #     default_available=True,
    #     timeout=60,
    #     thinking='low'
    # )

    # --------------------------------------------------------

    return {
        'sf_client_default': sf_client_default,
        'sf_client_a': sf_client_a,
        'sf_client_b': sf_client_b,
        'ms_client': ms_client,
        'zhipu_client': zhipu_client,
        'longcat': longcat_client,
        'gemini': gemini_client_1,
        # 'openclaw': openclaw_client,
    }


AI_CLIENTS = build_ai_clients()

AI_CLIENT_LIMIT = {
    'zhipu': 1,
    'model scope': 1,
    'silicon flow': 2,
    'longcat_client': 2,
    'silicon flow proxy': 1,
}


# --------------------------------------------------------------------------------
# Agent CLI (Harness) Clients —— 基于本机命令行 AI Agent 的客户端
# --------------------------------------------------------------------------------
# 适用于：API 服务不可用 / 余额查询失效时，改走本机已登录的 Agent CLI
# （Codex / Kimi 等）进行分析。认证由 CLI 自身管理，无可靠"余额"概念；
# 默认 unknown 预算策略会正常参与调度。若要控制本地花费，请使用 soft_limit
# （降权、不阻断）或 hard_limit（明确达到本地上限后阻断）。
#
# 两种模式：
#   MODE_STATELESS - 每次调用全新进程，完整 messages 序列化注入（模拟 AI Client）
#   MODE_SESSION   - 沿 Agent 会话续接调用，利用服务端缓存降低 Token 消耗
#                    （实测 codex 续接第二轮 cached_input_tokens 31872）
#
# 详见 AIClientCenter/doc/AgentCLIResearch.md
#
# 使用示例（取消注释并加入上面的 return 字典）：
#
# from AIClientCenter.AgentHarnessClients import (
#     CodexCLIClient, KimiCLIClient, MODE_STATELESS, MODE_SESSION,
# )
#
# codex_client = CodexCLIClient(
#     name='Codex CLI Session',
#     mode=MODE_SESSION,                # 或 MODE_STATELESS
#     # model='gpt-5-codex',            # 不指定则用 CLI 默认模型
#     work_dir=r'C:\D\code\IntelligenceIntegrationSystem',
#     priority=CLIENT_PRIORITY_CONSUMABLES,
#     group_id='agent_cli',
#     budget_policy=BudgetPolicy(BudgetMode.SOFT_LIMIT, {'total_tokens': 500000}),
#     active_health_checks=False,       # 默认值；避免定期探测消耗套餐
# )
#
# kimi_client = KimiCLIClient(
#     name='Kimi CLI Stateless',
#     mode=MODE_STATELESS,
#     priority=CLIENT_PRIORITY_CONSUMABLES,
#     group_id='agent_cli',
#     budget_policy=BudgetPolicy(BudgetMode.SOFT_LIMIT, {'total_tokens': 500000}),
# )
#
# 注意：session 模式的客户端同一时刻只能处理一个会话序列，
# 建议 group_id 独立并设置 AI_CLIENT_LIMIT['agent_cli'] = 1 控制并发。
