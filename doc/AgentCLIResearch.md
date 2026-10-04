# Agent CLI 非交互调用方式调研

## 2026-10-03 审计更新

本文件保留 2026-09-17 的人工调研记录，便于追溯当时的 CLI 行为；其中的
旧 Codex JSONL 事件名称和“quota_config 驱动健康度”的描述已经过时。

- 当前正式实现只有 CodexCLIClient 与 KimiCLIClient，代码位于 harness/cli.py。
- 当前本机验证的 Codex CLI 为 0.160.0。适配器解析 session_meta 与 event_msg
  包装的 item_completed、token_count、task_complete 事件；应以适配器和
  tests/test_harness_and_api_adapters.py 为准。
- Harness 默认采用 unknown 预算策略，缺少余额信息不会被排除；如需本地控制，
  使用 BudgetPolicy 的 soft_limit 或 hard_limit，而非把额度映射为 runtime health。
- Claude Code、Gemini CLI、Qwen Code、OpenCode、Aider 在下文只是扩展调研，
  不是当前已实现或已测试的 adapter。
- 对独立启动、配置校验和手动调用页面，请参阅 README；文件状态请参阅 FileGuide.md。

---

> 调研日期：2026-09-17。目的：将命令行 AI Agent 包装为 AIClientCenter 的分析客户端，
> 支持（1）模拟 AI Client 的单次调用、（2）沿上下文会话续接以利用缓存降低 Token 消耗。
>
> 标注说明：**【已实测】** = 在本机（Windows 11）真实调用验证；**【文档】** = 仅依据官方文档，未实测。

## 结果总览

| Agent | 非交互调用 | 会话续接 | 结构化输出 / 用量 | 安装情况 |
|---|---|---|---|---|
| Codex CLI | `codex exec -`（stdin 注入）**【已实测】** | `codex exec resume <thread_id> -` **【已实测】** | `--json` JSONL 事件流含 token usage **【已实测】** | 本机已装 0.154.0 |
| Kimi CLI | `kimi --quiet`（stdin 注入）**【已实测】** | `kimi --session <uuid> --quiet` **【已实测】** | 退出码语义化（0/1/75）**【已实测】** | 本机已装 1.37.0 |
| Claude Code | `claude -p "prompt"` 【文档】 | `--continue` / `--resume <session-id>` 【文档】 | `--output-format json`（含 usage / total_cost_usd / session_id）【文档】 | 未安装 |
| Gemini CLI | `gemini -p "prompt"` 【文档】 | `--resume <id\|latest>` 【文档】 | `--output-format json`（含 token stats）【文档】 | 未安装 |
| Qwen Code | `qwen -p "prompt"` 【文档】 | 同 Gemini（fork）【文档】 | 同 Gemini 【文档】 | 未安装 |
| OpenCode | `opencode run "msg"` 【文档】 | `-c` 继续 / `-s <session-id>` 【文档】 | `--print-logs` 等 【文档】 | 未安装 |
| Aider | `aider --message "..."` 【文档】 | `--restore-chat-history` 【文档】 | 无结构化输出，较弱 | 未安装 |

## Codex CLI（已实测）

- 调用：`echo "<prompt>" | codex exec - --json -o <tmpfile> -s read-only --skip-git-repo-check -C <workdir> [-m model]`
- 续接：`echo "<prompt>" | codex exec resume <thread_id> - --json -o <tmpfile> --skip-git-repo-check`
  - 注意：`resume` 子命令**不支持** `-s/--sandbox`、`-C/--cd`、`--color`，会话沿用首次调用时的配置。
- JSONL 事件（stdout）：
  - `{"type":"thread.started","thread_id":"..."}` → 会话 ID，供续接使用
  - `{"type":"item.completed","item":{"type":"agent_message","text":"..."}}` → 最终回复（也可读 `-o` 输出文件）
  - `{"type":"turn.completed","usage":{"input_tokens":N,"cached_input_tokens":M,"output_tokens":K,...}}` → 官方用量
- **缓存验证**：续接第二轮实测 `cached_input_tokens=31872`，缓存机制确实生效。
- 认证：走 `codex login` 的本机凭证（`~/.codex`），与 API Key 体系无关，天然绕开余额查询问题。

## Kimi CLI（已实测）

- 调用：`echo "<prompt>" | kimi --quiet [-m model] [-w workdir]`
  - `--quiet` = `--print --output-format text --final-message-only`，只输出最终消息
  - print 模式自动 auto-approve 所有工具调用；如需要可加 `--max-steps-per-turn 1` 限制单步
- 续接：`kimi --quiet --session <uuid>`
  - **session ID 可由调用方自行生成 UUID**（ID 不存在时自动创建新会话），首调与续接走同一命令路径，实现最简单。
- 退出码：`0` 成功；`1` 永久错误（配置/认证/配额）；`75` 瞬时错误（429/5xx/超时，可重试）。
- **编码陷阱（Windows）**：kimi 按系统 locale（中文 Windows 为 cp936）解码 stdin、编码 stdout，
  直接送 UTF-8 字节会报 `UnicodeEncodeError: surrogates not allowed`。
  解决：stdin 用 `locale.getpreferredencoding(False)` 编码、stdout 同编码解码（已在 `KimiCLIClient` 中处理）。
  Codex（Node.js）无此问题，恒为 UTF-8。
- 会话结束时 stderr 会打印 `To resume this session: kimi -r <id>`，也可作为 ID 发现手段。

## 接入约定（AIClientCenter）

- 消费方只认 OpenAI 风格响应：`response['choices'][0]['message']['content']`，`usage` 字段进 `record_usage()`。
- Agent CLI 客户端无"余额"概念，用 `quota_config`（用量配额）驱动健康度，绕开余额接口。
- 错误分类沿用 `APIResult` 体系：`PERMANENT` / `TRANSIENT_SERVER` / `TRANSIENT_NETWORK`。
- Windows 下 prompt 一律走 stdin；`.cmd` 封装（npm 全局安装的 codex）需 `cmd.exe /c` 中转。

## 未安装 Agent 的调用方式备忘（来自官方文档，供后续扩展）

### Claude Code
```bash
claude -p "prompt" --output-format json --max-turns 1 --model <model>
claude -p "follow-up" --continue                 # 继续最近会话
claude -p "follow-up" --resume <session-id>      # 恢复指定会话
```
JSON 输出含 `session_id`、`usage`、`total_cost_usd`（可直接折算成本）。

### Gemini CLI
```bash
gemini -p "prompt" --output-format json -m <model>
gemini -p "follow-up" --resume latest
```
JSON 输出含 response 与 token 统计。

### OpenCode
```bash
opencode run "prompt" -m provider/model
opencode run "follow-up" -c                      # 继续
opencode run "follow-up" -s <session-id>         # 指定会话
```

### Aider
```bash
aider --message "prompt" --yes-always --no-git --no-auto-commits
aider --message "follow-up" --restore-chat-history
```
会话恢复基于 chat history 文件，粒度较粗，不推荐作为首选。
