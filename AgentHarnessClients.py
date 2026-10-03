"""
Agent Harness Clients
=====================

将本机安装的命令行 AI Agent（Codex CLI、Kimi CLI 等）包装为 BaseAIClient，
使其可以注册进 AIClientManager，像普通 OpenAI 客户端一样参与调度。

两种工作模式（mode 参数）：

1. stateless（模拟 AI Client 调用）：
   每次 chat() 都启动一个全新的 CLI 进程，完整 messages 序列化为单个 Prompt
   通过 stdin 注入。互不干扰，无上下文。

2. session（沿上下文调用）：
   复用 CLI 的会话续接机制（codex exec resume / kimi --session），
   后续调用只发送增量消息，利用服务端 Prompt 缓存降低 Token 消耗。
   当检测到调用方传入的消息序列与已发送历史发生分叉时，自动开启新会话
   并重发完整上下文（自愈）。

注意：
- 此类客户端没有可验证的余额概念，默认使用 unknown 预算策略；可选地传入
  BudgetPolicy 配置本地 soft/hard 限额，但额度状态不参与运行时健康度。
- Windows 下统一通过 stdin 传 Prompt，避免命令行长度限制与引号转义问题。
- .cmd/.bat 封装（如 npm 全局安装的 codex）自动通过 cmd.exe /c 调用。
"""

import json
import locale
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    from .APIResult import APIResult
    from .LimitMixins import ClientMetricsMixin
    from .BudgetPolicy import BudgetPolicy
    from .AIClientManager import (
        BaseAIClient, ClientVisibility, CLIENT_PRIORITY_NORMAL,
    )
except ImportError:
    from APIResult import APIResult
    from LimitMixins import ClientMetricsMixin
    from BudgetPolicy import BudgetPolicy
    from AIClientManager import (
        BaseAIClient, ClientVisibility, CLIENT_PRIORITY_NORMAL,
    )

logger = logging.getLogger(__name__)

MODE_STATELESS = 'stateless'
MODE_SESSION = 'session'


def serialize_messages(messages: List[Dict[str, str]]) -> str:
    """将 OpenAI 风格 messages 序列化为单个 Prompt 文本。"""
    parts = []
    for m in messages:
        role = m.get('role', 'user')
        content = m.get('content', '')
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    """无官方用量数据时的粗略估算（中英文混合，约 4 字符 1 token）。"""
    return max(1, len(text) // 4)


class AgentCLIClient(ClientMetricsMixin, BaseAIClient):
    """
    命令行 Agent 客户端基类。

    子类需覆盖：
        _build_exec_command(model)            -> List[str]   全新调用命令
        _build_resume_command(session_id, model) -> List[str] 会话续接命令（可选，无会话能力则不覆盖）
        _parse_output(stdout, stderr, final_text_file) -> (text, usage_dict, session_id)
        _classify_error(exit_code, stdout, stderr) -> error dict
    """

    #: 子类覆盖：CLI 提供方标识，用于 get_api_base_url()
    CLI_PROVIDER = 'agent-cli'
    #: 子类覆盖：是否支持会话续接
    SUPPORTS_SESSION = True
    #: 子类覆盖：子进程 stdin/stdout 编码。
    #: Node 系 CLI（codex）恒为 utf-8；Python 系 CLI（kimi）随系统 locale（如 cp936）。
    STDIN_ENCODING = 'utf-8'
    STDOUT_ENCODING = 'utf-8'

    def __init__(
            self,
            name: str,
            cli_command: str,
            model: Optional[str] = None,
            work_dir: Optional[str] = None,
            mode: str = MODE_STATELESS,
            timeout: float = 300.0,
            priority: int = CLIENT_PRIORITY_NORMAL,
            group_id: str = 'agent_cli',
            visibility: ClientVisibility = ClientVisibility.PUBLIC,
            quota_config: Optional[Dict[str, Any]] = None,
            balance_config: Optional[Dict[str, float]] = None,
            state_file_path: Optional[str] = None,
            budget_policy: Optional[BudgetPolicy] = None,
            active_health_checks: bool = False,
            extra_env: Optional[Dict[str, str]] = None,
    ):
        super().__init__(
            name=name,
            api_token='cli-harness',  # CLI 认证由 Agent 自身管理，此处仅占位
            priority=priority,
            group_id=group_id,
            visibility=visibility,
            quota_config=quota_config,
            balance_config=balance_config,
            state_file_path=state_file_path,
            budget_policy=budget_policy,
        )

        if mode == MODE_SESSION and not self.SUPPORTS_SESSION:
            logger.warning(f'{self.CLI_PROVIDER} 不支持会话续接，回退为 stateless 模式')
            mode = MODE_STATELESS
        if mode not in (MODE_STATELESS, MODE_SESSION):
            raise ValueError(f"mode 必须是 '{MODE_STATELESS}' 或 '{MODE_SESSION}'")

        self.cli_command = self._resolve_cli_command(cli_command)
        self.model = model
        self.work_dir = work_dir or os.getcwd()
        self.mode = mode
        self.timeout = timeout
        # Harness 自测本身会消耗套餐额度，默认仅靠真实请求和人工检查观测状态。
        self.active_health_checks = active_health_checks
        self.extra_env = dict(extra_env or {})

        # 会话状态（session 模式）
        self._session_id: Optional[str] = None
        self._session_history: List[Dict[str, str]] = []
        self._session_lock = threading.Lock()

    # -------------------------------------- 命令行解析 --------------------------------------

    @staticmethod
    def _resolve_cli_command(cli_command: str) -> str:
        """解析 CLI 可执行文件路径。Windows 下优先 .exe，其次 .cmd/.bat。"""
        if os.path.sep in cli_command or (os.path.altsep and os.path.altsep in cli_command):
            return cli_command
        if os.name == 'nt':
            for ext in ('.exe', '.cmd', '.bat'):
                found = shutil.which(cli_command + ext)
                if found:
                    return found
        return shutil.which(cli_command) or cli_command

    def _wrap_command(self, args: List[str]) -> List[str]:
        """.cmd/.bat 在 Windows 上不能直接被 CreateProcess 执行，需要 cmd.exe 中转。"""
        exe = self.cli_command
        if os.name == 'nt' and exe.lower().endswith(('.cmd', '.bat')):
            return ['cmd.exe', '/c', exe] + args
        return [exe] + args

    def _build_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        # 统一子进程输出编码，抑制彩色控制字符
        if self.STDIN_ENCODING == 'utf-8':
            env.setdefault('PYTHONUTF8', '1')
            env.setdefault('PYTHONIOENCODING', 'utf-8')
        env.setdefault('NO_COLOR', '1')
        env.update(self.extra_env)
        return env

    # -------------------------------------- 子类钩子 --------------------------------------

    def _build_exec_command(self, model: Optional[str], final_text_file: Optional[str]) -> List[str]:
        raise NotImplementedError

    def _build_resume_command(self, session_id: str, model: Optional[str],
                              final_text_file: Optional[str]) -> List[str]:
        raise NotImplementedError

    def _parse_output(self, stdout: str, stderr: str,
                      final_text_file: Optional[str]) -> Tuple[str, Dict[str, int], Optional[str]]:
        """返回 (最终文本, usage 字典, 本次会话ID)。子类按需覆盖。"""
        return stdout.strip(), {}, None

    def _classify_error(self, exit_code: Optional[int], stdout: str, stderr: str) -> Dict[str, Any]:
        """将非零退出码映射为 APIResult error。通用实现基于关键词，子类可覆盖。"""
        blob = f'{stdout}\n{stderr}'.lower()

        if exit_code is None:
            return {'type': 'TRANSIENT_NETWORK', 'code': 'CONNECTION_TIMEOUT',
                    'message': 'CLI process timed out.'}
        if '429' in blob or 'rate limit' in blob or 'too many requests' in blob:
            return {'type': 'TRANSIENT_SERVER', 'code': 'HTTP_429',
                    'message': f'Rate limited. {stderr[:200]}'}
        if '401' in blob or '403' in blob or 'unauthorized' in blob or 'authentication' in blob:
            return {'type': 'PERMANENT', 'code': 'HTTP_401',
                    'message': f'Authentication failed. {stderr[:200]}'}
        # 默认按瞬时错误处理（交给健康检查恢复），避免一次未知失败就永久下线
        return {'type': 'TRANSIENT_SERVER', 'code': f'CLI_EXIT_{exit_code}',
                'message': f'CLI exited with code {exit_code}. {stderr[:200]}'}

    # -------------------------------------- 进程执行 --------------------------------------

    def _run_cli(self, args: List[str], prompt: str,
                 final_text_file: Optional[str]) -> Tuple[Optional[int], str, str]:
        """
        执行 CLI 进程，prompt 走 stdin。
        返回 (exit_code, stdout, stderr)；超时时 exit_code 为 None。
        """
        cmd = self._wrap_command(args)
        logger.debug(f'[{self.name}] run: {" ".join(cmd[:3])} ...')

        try:
            proc = subprocess.run(
                cmd,
                # stdin 编码必须与子进程的解码方式匹配，否则中文等非 ASCII 内容会损坏
                #（典型症状：子进程报 UnicodeEncodeError surrogates not allowed）
                input=prompt.encode(self.STDIN_ENCODING, errors='replace'),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.work_dir if os.path.isdir(self.work_dir) else None,
                env=self._build_env(),
                timeout=self.timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            stdout = proc.stdout.decode(self.STDOUT_ENCODING, errors='replace')
            stderr = proc.stderr.decode(self.STDOUT_ENCODING, errors='replace')
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            logger.error(f'[{self.name}] CLI timeout after {self.timeout}s')
            return None, '', f'Timeout after {self.timeout}s'
        except FileNotFoundError as e:
            return 127, '', f'CLI not found: {e}'
        except Exception as e:
            logger.exception(f'[{self.name}] CLI execution failed')
            return 1, '', f'CLI execution exception: {e}'

    # -------------------------------------- 会话管理 --------------------------------------

    def _new_session_id(self) -> str:
        """生成新会话 ID。kimi 等支持外部指定 ID 的 CLI 可直接使用。"""
        return str(uuid.uuid4())

    def _reset_session(self):
        self._session_id = None
        self._session_history = []

    @staticmethod
    def _common_prefix_len(a: List[Dict[str, str]], b: List[Dict[str, str]]) -> int:
        n = 0
        for x, y in zip(a, b):
            if x.get('role') != y.get('role') or x.get('content') != y.get('content'):
                break
            n += 1
        return n

    def _split_session_prompt(self, messages: List[Dict[str, str]]) -> Tuple[bool, str]:
        """
        session 模式下计算本次应发送的内容。
        返回 (is_resume, prompt)：
        - 历史为空 -> (False, 完整序列化)
        - 新消息是历史的延伸 -> (True, 增量序列化)
        - 发生分叉 -> 重置会话，(False, 完整序列化)
        """
        if not self._session_id or not self._session_history:
            return False, serialize_messages(messages)

        common = self._common_prefix_len(self._session_history, messages)
        if common == len(self._session_history) and len(messages) > len(self._session_history):
            delta = messages[len(self._session_history):]
            return True, serialize_messages(delta)

        logger.warning(f'[{self.name}] 会话上下文分叉，重置会话并重发完整上下文')
        self._reset_session()
        return False, serialize_messages(messages)

    # -------------------------------------- BaseAIClient 接口 --------------------------------------

    def get_model_list(self) -> Dict[str, Any]:
        return {self.model or 'default': {'owned_by': self.CLI_PROVIDER, 'source': 'cli'}}

    def get_current_model(self) -> str:
        return self.model or 'default'

    def get_api_base_url(self) -> str:
        return f'cli://{self.CLI_PROVIDER}'

    def get_session_id(self) -> Optional[str]:
        """当前会话 ID（session 模式可用），供调试/外部续接使用。"""
        return self._session_id

    def _chat_completion_sync(self,
                              messages: List[Dict[str, str]],
                              model: Optional[str] = None,
                              temperature: float = 0.7,
                              max_tokens: int = 4096,
                              is_health_check: bool = False) -> APIResult:
        use_model = model or self.model

        # 健康检查不污染会话，一律走无状态调用
        if is_health_check or self.mode == MODE_STATELESS:
            return self._invoke(messages, use_model, is_resume=False, track_session=False)

        with self._session_lock:
            is_resume, prompt = self._split_session_prompt(messages)
            result = self._invoke_with_prompt(prompt, use_model, is_resume=is_resume)

            # resume 失败（会话丢失/过期等）：重置会话，携带完整上下文重试一次
            if not result.get('success') and is_resume:
                logger.warning(f'[{self.name}] resume 失败，重置会话后重试')
                self._reset_session()
                result = self._invoke_with_prompt(
                    serialize_messages(messages), use_model, is_resume=False)

            if result.get('success'):
                self._session_history = [dict(m) for m in messages]

            return result

    # -------------------------------------- 调用实现 --------------------------------------

    def _invoke(self, messages: List[Dict[str, str]], model: Optional[str],
                is_resume: bool, track_session: bool) -> APIResult:
        with self._session_lock:
            result = self._invoke_with_prompt(serialize_messages(messages), model, is_resume=False)
            if result.get('success') and track_session:
                self._session_history = [dict(m) for m in messages]
            return result

    def _invoke_with_prompt(self, prompt: str, model: Optional[str], is_resume: bool) -> APIResult:
        final_text_file = self._make_temp_file()
        try:
            if is_resume and self._session_id:
                args = self._build_resume_command(self._session_id, model, final_text_file)
            else:
                args = self._build_exec_command(model, final_text_file)

            exit_code, stdout, stderr = self._run_cli(args, prompt, final_text_file)

            if exit_code != 0:
                return {'success': False, 'data': None,
                        'error': self._classify_error(exit_code, stdout, stderr)}

            text, usage, session_id = self._parse_output(stdout, stderr, final_text_file)

            if not text:
                return {'success': False, 'data': None,
                        'error': {'type': 'TRANSIENT_SERVER', 'code': 'EMPTY_OUTPUT',
                                  'message': f'CLI 退出码为 0 但未产生输出。stderr: {stderr[:200]}'}}

            if session_id and self.mode == MODE_SESSION:
                self._session_id = session_id

            usage = self._normalize_usage(usage, prompt, text)
            return {'success': True, 'data': self._wrap_openai_response(text, usage, model),
                    'error': None}
        finally:
            self._cleanup_temp_file(final_text_file)

    def _normalize_usage(self, usage: Dict[str, int], prompt: str, text: str) -> Dict[str, int]:
        """CLI 未上报 usage 时按字符数估算，保证 record_usage 有数据可用。"""
        usage = dict(usage or {})
        if not usage.get('prompt_tokens'):
            usage['prompt_tokens'] = estimate_tokens(prompt)
        if not usage.get('completion_tokens'):
            usage['completion_tokens'] = estimate_tokens(text)
        usage['total_tokens'] = usage.get('total_tokens') or (
                usage['prompt_tokens'] + usage['completion_tokens'])
        return usage

    @staticmethod
    def _wrap_openai_response(text: str, usage: Dict[str, int], model: Optional[str]) -> Dict[str, Any]:
        return {
            'id': f'agentcli-{uuid.uuid4()}',
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': model or 'agent-cli',
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': text},
                'finish_reason': 'stop',
            }],
            'usage': usage,
        }

    @staticmethod
    def _make_temp_file() -> Optional[str]:
        return None  # 子类如需要 -o 输出文件则覆盖

    @staticmethod
    def _cleanup_temp_file(path: Optional[str]):
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


# ======================================================================================
# Codex CLI
# ======================================================================================

class CodexCLIClient(AgentCLIClient):
    """
    OpenAI Codex CLI 客户端。

    验证环境：codex-cli 0.154.0（Windows）。

    无状态调用：
        codex exec - --json -o <tmpfile> -s read-only --skip-git-repo-check -C <workdir> [-m model]
        （Prompt 通过 stdin 传入）

    会话续接：
        codex exec resume <thread_id> - --json -o <tmpfile> --skip-git-repo-check
        注意：resume 子命令不支持 -s/--sandbox 与 -C/--cd，会话会沿用首次调用时的配置。

    事件流（--json，JSONL）：
        session_meta -> payload.session_id（会话 ID）
        event_msg    -> payload.type=item_completed 时的 AgentMessage 文本
        event_msg    -> payload.type=token_count 时的 token 用量
    """

    CLI_PROVIDER = 'codex'
    SUPPORTS_SESSION = True

    def __init__(self, name: str, cli_command: str = 'codex',
                 sandbox: str = 'read-only', **kwargs):
        self.sandbox = sandbox
        super().__init__(name=name, cli_command=cli_command, **kwargs)

    def _common_args(self, model: Optional[str], final_text_file: Optional[str]) -> List[str]:
        args = ['--json', '--skip-git-repo-check']
        if final_text_file:
            args += ['-o', final_text_file]
        if model:
            args += ['-m', model]
        return args

    def _build_exec_command(self, model: Optional[str], final_text_file: Optional[str]) -> List[str]:
        args = ['exec', '-'] + self._common_args(model, final_text_file)
        if self.sandbox:
            args += ['-s', self.sandbox]
        args += ['-C', self.work_dir]
        return args

    def _build_resume_command(self, session_id: str, model: Optional[str],
                              final_text_file: Optional[str]) -> List[str]:
        # resume 不支持 -s / -C / --color，沿用首次会话的配置
        return ['exec', 'resume', session_id, '-'] + self._common_args(model, final_text_file)

    @staticmethod
    def _make_temp_file() -> Optional[str]:
        fd, path = tempfile.mkstemp(prefix='codex_out_', suffix='.txt')
        os.close(fd)
        return path

    def _parse_output(self, stdout: str, stderr: str,
                      final_text_file: Optional[str]) -> Tuple[str, Dict[str, int], Optional[str]]:
        session_id = None
        usage: Dict[str, int] = {}
        last_agent_text = ''

        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get('type')
            payload = event.get('payload') or {}
            if etype == 'session_meta':
                session_id = payload.get('session_id') or payload.get('id') or session_id
            elif etype == 'event_msg' and payload.get('type') == 'item_completed':
                item = payload.get('item') or {}
                if item.get('type') in ('AgentMessage', 'agent_message'):
                    last_agent_text = item.get('text')
                    if not last_agent_text:
                        last_agent_text = ''.join(
                            str(part.get('text', ''))
                            for part in item.get('content') or []
                            if isinstance(part, dict)
                        )
                if last_agent_text:
                    last_agent_text = last_agent_text.strip()
            elif etype == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info') or {}
                u = info.get('last_token_usage') or info.get('total_token_usage') or {}
                if u:
                    usage = {
                        'prompt_tokens': u.get('input_tokens', 0),
                        'completion_tokens': u.get('output_tokens', 0),
                        'total_tokens': u.get('input_tokens', 0) + u.get('output_tokens', 0),
                        'cached_input_tokens': u.get('cached_input_tokens', 0),
                    }
            elif etype == 'event_msg' and payload.get('type') == 'task_complete':
                last_agent_text = payload.get('last_agent_message') or last_agent_text

        # 最终文本优先取 -o 输出文件
        text = ''
        if final_text_file and os.path.isfile(final_text_file):
            try:
                with open(final_text_file, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read().strip()
            except OSError:
                pass
        if not text:
            text = last_agent_text.strip()

        return text, usage, session_id

    def _classify_error(self, exit_code: Optional[int], stdout: str, stderr: str) -> Dict[str, Any]:
        blob = f'{stdout}\n{stderr}'.lower()

        # Parse Codex JSONL errors first. Request IDs may contain 401.
        for line in stdout.splitlines():
            if not line.startswith('{'):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            payload = event.get('payload') or {}
            if payload.get('type') != 'task_complete' or not isinstance(payload.get('error'), dict):
                continue

            raw_message = str(payload['error'].get('message', ''))
            try:
                detail = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}

            if isinstance(detail.get('error'), dict):
                detail = detail['error']

            code = detail.get('code') or payload['error'].get('code') or 'CLI_ERROR'
            error_type = detail.get('type') or payload['error'].get('type') or ''
            message = detail.get('message') or raw_message
            if error_type == 'BadRequest' or code == 'InvalidParameter':
                return {
                    'type': 'BAD_REQUEST',
                    'code': 'HTTP_400',
                    'message': f'{message} (code={code})',
                }

        if exit_code is not None and ('no session' in blob or 'not found' in blob) and 'resume' in blob:
            # 会话丢失：标记为瞬时错误，上层 _chat_completion_sync 会自动重建会话重试
            return {'type': 'TRANSIENT_SERVER', 'code': 'SESSION_LOST',
                    'message': f'Codex session lost. {stderr[:200]}'}
        return super()._classify_error(exit_code, stdout, stderr)


# ======================================================================================
# Kimi CLI
# ======================================================================================

class KimiCLIClient(AgentCLIClient):
    """
    Kimi Code CLI 客户端。

    验证环境：kimi 1.37.0（Windows）。

    无状态调用：
        kimi --quiet [-m model] [-w workdir]
        （Prompt 通过 stdin 传入；--quiet = --print --output-format text --final-message-only）

    会话续接：
        kimi --quiet --session <uuid> ...
        Kimi 对不存在的 session ID 会自动创建新会话，因此会话 ID 由本类生成 UUID，
        首次调用与续接走同一条命令路径。

    退出码（官方文档）：
        0  成功
        1  永久错误（配置/认证/配额，不可重试）
        75 瞬时错误（429 限速 / 5xx / 网络超时，可重试）
    """

    CLI_PROVIDER = 'kimi'
    SUPPORTS_SESSION = True

    EXIT_CODE_TRANSIENT = 75

    # Kimi CLI（Python 系）按系统 locale 解码 stdin / 编码 stdout，
    # Windows 中文环境下为 cp936；UTF-8 locale 环境下自然为 utf-8。
    STDIN_ENCODING = locale.getpreferredencoding(False) or 'utf-8'
    STDOUT_ENCODING = STDIN_ENCODING

    def __init__(self, name: str, cli_command: str = 'kimi',
                 max_steps_per_turn: Optional[int] = None, **kwargs):
        self.max_steps_per_turn = max_steps_per_turn
        super().__init__(name=name, cli_command=cli_command, **kwargs)
        if self.mode == MODE_SESSION and not self._session_id:
            # Kimi 允许外部指定 session ID，提前生成，首调即绑定
            self._session_id = self._new_session_id()

    def _common_args(self, model: Optional[str]) -> List[str]:
        args = ['--quiet', '-w', self.work_dir]
        if model:
            args += ['-m', model]
        if self.max_steps_per_turn:
            args += ['--max-steps-per-turn', str(self.max_steps_per_turn)]
        return args

    def _build_exec_command(self, model: Optional[str], final_text_file: Optional[str]) -> List[str]:
        args = self._common_args(model)
        if self.mode == MODE_SESSION and self._session_id:
            args += ['--session', self._session_id]
        return args

    def _build_resume_command(self, session_id: str, model: Optional[str],
                              final_text_file: Optional[str]) -> List[str]:
        return self._common_args(model) + ['--session', session_id]

    def _parse_output(self, stdout: str, stderr: str,
                      final_text_file: Optional[str]) -> Tuple[str, Dict[str, int], Optional[str]]:
        # --quiet 模式下 stdout 即最终消息；usage 不上报，由基类估算
        return stdout.strip(), {}, self._session_id

    def _classify_error(self, exit_code: Optional[int], stdout: str, stderr: str) -> Dict[str, Any]:
        if exit_code is None:
            return {'type': 'TRANSIENT_NETWORK', 'code': 'CONNECTION_TIMEOUT',
                    'message': 'Kimi CLI process timed out.'}
        if exit_code == self.EXIT_CODE_TRANSIENT:
            return {'type': 'TRANSIENT_SERVER', 'code': 'KIMI_TRANSIENT_75',
                    'message': f'Kimi transient error (rate limit / 5xx / timeout). {stderr[:200]}'}
        if exit_code == 1:
            return {'type': 'PERMANENT', 'code': 'KIMI_PERMANENT_1',
                    'message': f'Kimi permanent error (config/auth/quota). {stderr[:200]}'}
        return super()._classify_error(exit_code, stdout, stderr)


# ======================================================================================
# 便捷工厂
# ======================================================================================

def create_codex_client(name: str = 'Codex CLI', mode: str = MODE_STATELESS,
                        model: Optional[str] = None, **kwargs) -> CodexCLIClient:
    return CodexCLIClient(name=name, model=model, mode=mode, **kwargs)


def create_kimi_client(name: str = 'Kimi CLI', mode: str = MODE_STATELESS,
                       model: Optional[str] = None, **kwargs) -> KimiCLIClient:
    return KimiCLIClient(name=name, model=model, mode=mode, **kwargs)
