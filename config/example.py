# --------------------------------------------------------------------------------
# 使用 Python 配置，便于直接构造并组合 Client。
# 首次使用请复制本文件为同目录 config.py；该本机文件不会提交到 Git。
# --------------------------------------------------------------------------------
import os
from typing import List, Dict

from GlobalConfig import *
from AIClientCenter.providers.zhipu import ZhipuSDKAdapter
from AIClientCenter.providers.openai_clients import StandardOpenAIClient, \
    SelfRotatingOpenAIClient, OuterTokenRotatingOpenAIClient
from AIClientCenter.core.manager import CLIENT_PRIORITY_EXPENSIVE, \
    CLIENT_PRIORITY_FREEBIE, BaseAIClient, CLIENT_PRIORITY_NORMAL, CLIENT_PRIORITY_CONSUMABLES
from AIClientCenter.providers.openai_compatible import create_siliconflow_client, create_modelscope_client, \
    create_long_cat_client
from AIClientCenter.services.token_rotator import SiliconFlowServiceRotator
from AIClientCenter.providers.gemini import GoogleGeminiAdapter
from AIClientCenter.providers.openclaw import OpenClawClient
from AIClientCenter.core.budget import BudgetMode, BudgetPolicy
from AIClientCenter.harness.cli import (
    CodexCLIClient, KimiCLIClient, MODE_SESSION, MODE_STATELESS,
)


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

    zhipu_client = None
    zhipu_api_key = os.getenv('ZHIPU_API_KEY')
    if zhipu_api_key:
        zhipu_adapter = ZhipuSDKAdapter(
            api_key=zhipu_api_key,
            enable_thinking=False
        )
        zhipu_client = StandardOpenAIClient(
            name="zhipu-01",
            openai_api=zhipu_adapter,
            priority=CLIENT_PRIORITY_CONSUMABLES,
            group_id='zhipu',
            default_available=True
        )
    else:
        # 未配置则不实例化 SDK，保证样例可在未安装可选依赖时完成校验。
        # 复制为 config.py 后设置 ZHIPU_API_KEY 即可启用。
        print("配置提示：未设置 ZHIPU_API_KEY，跳过智谱 Client。")

    # --------------------------------------------------------

    gemini_api_1 = GoogleGeminiAdapter(
        api_key=os.getenv('GEMINI_API_KEY', 'your-gemini-key'),
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

    # ========================================================================
    # 预算策略配置示例
    # ========================================================================
    # 所有 Client 都可以设置 budget_policy；它只影响本地调度，绝不改变
    # Client 的运行时状态。limits 可使用 total_tokens、request_count、
    # prompt_tokens、completion_tokens 等由响应 usage 上报的数值字段。
    # period_days 是本地计数窗口；0 表示不自动重置。
    #
    # 1) UNKNOWN（默认）：余额/套餐额度未知，正常参与调度；适合 CLI Harness。
    #    budget_policy=BudgetPolicy(BudgetMode.UNKNOWN)
    #
    # 2) OBSERVED：只记录和展示用量，不影响调度；适合无法可靠查询余额的 API。
    #    budget_policy=BudgetPolicy(BudgetMode.OBSERVED)
    #
    # 3) SOFT_LIMIT：达到本地限额后排在正常候选之后；所有其他 client 都不可用
    #    时仍会使用。适合昂贵 API 或套餐型 Harness。
    #    budget_policy=BudgetPolicy(
    #        BudgetMode.SOFT_LIMIT,
    #        limits={'total_tokens': 500_000}, period_days=1,
    #        soft_limit_multiplier=0.1,
    #    )
    #
    # 4) HARD_LIMIT：达到本地限额后不再分配新任务；适合明确的免费调用次数。
    #    budget_policy=BudgetPolicy(
    #        BudgetMode.HARD_LIMIT,
    #        limits={'request_count': 495}, period_days=1,
    #    )
    #
    # 5) minimums：余额低于阈值时阻断（只有余额确实可查询/维护时使用）。
    #    budget_policy=BudgetPolicy(
    #        BudgetMode.HARD_LIMIT, minimums={'balance': 1.0},
    #    )

    # ---------------- Agent CLI / Harness 配置 ----------------
    # Harness 认证由本机 CLI 处理，运行前请先在终端完成对应 CLI 的登录。
    # 默认 active_health_checks=False，避免定期 "OK" 探测消耗套餐。
    # session 客户端只能串行使用；请给每个 session group 设置并发 1。

    # A. Codex：无状态模式。每篇情报独立进程，推荐用于普通新闻分析。
    # codex_stateless = CodexCLIClient(
    #     name='Codex CLI Stateless',
    #     mode=MODE_STATELESS,
    #     work_dir=os.getcwd(),          # 或填写 IIS 项目绝对路径
    #     priority=CLIENT_PRIORITY_CONSUMABLES,
    #     group_id='codex_cli',
    #     budget_policy=BudgetPolicy(BudgetMode.UNKNOWN),
    # )

    # B. Codex：session 模式。仅用于同一专题的连续推演，不能混用独立文章。
    # codex_session = CodexCLIClient(
    #     name='Codex CLI Session',
    #     mode=MODE_SESSION,
    #     work_dir=os.getcwd(),
    #     priority=CLIENT_PRIORITY_CONSUMABLES,
    #     group_id='codex_session',
    #     budget_policy=BudgetPolicy(
    #         BudgetMode.SOFT_LIMIT, limits={'total_tokens': 500_000}, period_days=1,
    #     ),
    # )

    # C. Kimi：无状态模式。若 CLI 不上报 token，框架按字符数估算并作为本地观测。
    # kimi_stateless = KimiCLIClient(
    #     name='Kimi CLI Stateless',
    #     mode=MODE_STATELESS,
    #     priority=CLIENT_PRIORITY_CONSUMABLES,
    #     group_id='kimi_cli',
    #     budget_policy=BudgetPolicy(
    #         BudgetMode.SOFT_LIMIT, limits={'request_count': 100}, period_days=1,
    #     ),
    # )

    # D. GLM：当前没有 GLM CLI Harness adapter。若使用 Zhipu/GLM 的 API，
    #    可按普通 StandardOpenAIClient/ZhipuSDKAdapter 配置并选择 OBSERVED、
    #    SOFT_LIMIT 或 HARD_LIMIT。若将来存在稳定的非交互 GLM CLI，应新增
    #    GLMCLIClient(AgentCLIClient) 后再在此处实例化，不能把 CLI 命令直接
    #    填进 StandardOpenAIClient。

    # E. 无余额 API 的观测模式示例：不因无法查询余额被调度器排除。
    # unknown_balance_client = StandardOpenAIClient(
    #     name='Unknown Balance API', openai_api=some_openai_api,
    #     priority=CLIENT_PRIORITY_EXPENSIVE, group_id='unknown_api',
    #     budget_policy=BudgetPolicy(BudgetMode.OBSERVED),
    # )

    # -------------- OpenClaw client --------------
    # - 通过 OpenClaw Gateway 转发给外部 Agent；它不是 AgentCLIClient。
    # - 它同样可以接收 budget_policy（取决于 OpenClawClient 构造函数版本）。
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

    clients = {
        'sf_client_default': sf_client_default,
        'sf_client_a': sf_client_a,
        'sf_client_b': sf_client_b,
        'ms_client': ms_client,
        'longcat': longcat_client,
        'gemini': gemini_client_1,
        # 'codex_stateless': codex_stateless,
        # 'codex_session': codex_session,
        # 'kimi_stateless': kimi_stateless,
        # 'unknown_balance': unknown_balance_client,
        # 'openclaw': openclaw_client,
    }
    if zhipu_client is not None:
        clients['zhipu_client'] = zhipu_client
    return clients


AI_CLIENTS = build_ai_clients()

AI_CLIENT_LIMIT = {
    'zhipu': 1,
    'model scope': 1,
    'silicon flow': 2,
    'longcat_client': 2,
    'silicon flow proxy': 1,
    # 'codex_cli': 1,       # 无状态也建议限制，避免同时占用多个套餐任务
    # 'codex_session': 1,   # 必须为 1：同一 session 不可并行
    # 'kimi_cli': 1,
}
