import os
import sys
import threading
import time
import random
import logging
import traceback
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor

self_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(self_path)

try:
    from ComplexConversation import MESSAGE
    from AIClientManagerBackend import AIDashboardService
    from ai_client_config_example import AI_CLIENTS, AI_CLIENT_LIMIT
    from AIClients import StandardOpenAIClient, SelfRotatingOpenAIClient, OuterTokenRotatingOpenAIClient
    from AIClientManager import CLIENT_PRIORITY_EXPENSIVE, AIClientManager, CLIENT_PRIORITY_FREEBIE
    from OpenAICompatibleAPI import create_siliconflow_client, create_modelscope_client
    from AIServiceTokenRotator import SiliconFlowServiceRotator
except ImportError as e:
    print(str(e))

    from .ComplexConversation import MESSAGE
    from .AIClientManagerBackend import AIDashboardService
    from .ai_client_config_example import AI_CLIENTS, AI_CLIENT_LIMIT
    from .AIClients import StandardOpenAIClient, SelfRotatingOpenAIClient, OuterTokenRotatingOpenAIClient
    from .AIClientManager import CLIENT_PRIORITY_EXPENSIVE, AIClientManager, CLIENT_PRIORITY_FREEBIE
    from .OpenAICompatibleAPI import create_siliconflow_client, create_modelscope_client
    from .AIServiceTokenRotator import SiliconFlowServiceRotator


# 1. 定义彩色格式
class ColoredFormatter(logging.Formatter):
    # ANSI 颜色代码
    GREY = "\x1b[38;20m"
    GREEN = "\x1b[32;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"

    fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    FORMATS = {
        logging.DEBUG: GREY + fmt + RESET,
        logging.INFO: GREEN + fmt + RESET,
        logging.WARNING: YELLOW + fmt + RESET,
        logging.ERROR: RED + fmt + RESET,
        logging.CRITICAL: BOLD_RED + fmt + RESET
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


# 2. 配置全局 Root Logger
def setup_colored_logging():
    # 获取 Root Logger (不加名字就是 Root)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)  # 设置全局级别

    # 清空已有的 Handler，防止 PyCharm 重复打印或冲突
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # 创建输出到 stdout 的处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter())

    # 添加到 Root Logger
    root_logger.addHandler(console_handler)


working_path = os.getcwd()
SYSTEM_PROMPT = '你是一个专业的智能人工助手。'


# ----------------------------------------------------------------------------------------------------------------------

# 100 个简短的 AI 测试问题
TEST_PROMPTS = [
    # 基础与闲聊
    "你好。", "你是谁？", "讲个笑话。", "今天天气怎么样？", "给我一个早安问候。",
    "你能听到我吗？", "唱首歌。", "你开心吗？", "你的名字是什么？", "再见。",

    # 常识与事实
    "法国首都是哪里？", "地球是圆的吗？", "谁写了《红楼梦》？", "水的化学式是什么？", "一年有几天？",
    "太阳从哪边升起？", "最大的海洋是哪个？", "蜘蛛有几条腿？", "冰融化变成什么？", "美国的货币是什么？",

    # 逻辑与数学
    "1 + 1 等于几？", "10 减 3 等于几？", "25 的平方根是多少？", "树上有10只鸟，打死1只，还剩几只？",
    "哪个更重，一斤铁还是一斤棉花？",
    "父亲的儿子是我的什么人？", "如果 A > B 且 B > C，A 和 C 谁大？", "找规律：1, 3, 5, 7, 下一个数是？", "什么是质数？",
    "三个苹果分给两个人，怎么分？",

    # 语言与翻译
    "把 'Hello' 翻译成中文。", "把 '谢谢' 翻译成英文。", "'Apple' 是什么意思？", "用“天空”造句。", "解释成语“画蛇添足”。",
    "Bonjour 是哪国语言？", "给“快乐”找个反义词。", "给“美丽”找个同义词。", "把 'I love coding' 翻译成日语。",
    "什么是动词？",

    # 编程与技术
    "写一个 Python 的 Hello World。", "什么是 HTML？", "Linux 列出文件的命令是什么？", "给出一个 JSON 示例。",
    "什么是 IP 地址？",
    "写一个死循环代码。", "解释 HTTP 404。", "SQL 中如何查询所有数据？", "什么是 Bug？", "推荐一种编程语言。",

    # 创意与写作
    "给我的猫起个名字。", "写一首关于雨的短诗。", "帮我想个咖啡店的名字。", "用三个词形容夏天。", "讲一个鬼故事（一句话）。",
    "夸我一句。", "假如你会飞，你会去哪？", "给我一个创业点子。", "写一句励志的话。", "形容一下蓝色的味道。",

    # 简短指令遵循
    "只回复“收到”。", "不要回复任何文字。", "把这句话大写：hello。", "重复我说的话：测试。", "输出数字 1 到 5。",
    "告诉我现在的年份。", "你的回答限制在 5 个字以内。", "用 JSON 格式回复“你好”。", "仅仅输出一个 Emoji。",
    "倒序拼写 'ABC'。",

    # 科学与自然
    "天空为什么是蓝的？", "什么是光合作用？", "恐龙还存在吗？", "速度最快的动物是什么？", "钻石是什么元素构成的？",
    "人的心脏在哪边？", "月亮自己发光吗？", "什么是引力？", "DNA 是什么？", "沸水是多少度？",

    # 生活与建议
    "怎么煮鸡蛋？", "推荐一部电影。", "怎么系鞋带？", "睡不着怎么办？", "感冒了喝什么？",
    "这是一个测试吗？", "什么是 AI？", "推荐一本书。", "怎么减肥？", "怎么交朋友？",

    # 随机与抽象
    "生命的意义是什么？", "先有鸡还是先有蛋？", "什么是爱？", "什么是时间？", "你有意识吗？",
    "什么是元宇宙？", "给我一个随机数。", "抛硬币是正面还是反面？", "什么是区块链？", "结束了吗？"
]


def get_random_test_prompt() -> str:
    """
    从测试列表中随机返回一个问题。
    """
    return random.choice(TEST_PROMPTS)


# ----------------------------------------------------------------------------------------------------------------------

def simple_chat(user_message: str, context: Optional[List[Dict[str, str]]] = None):
    messages = context if context else []
    messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.append({"role": "user", "content": user_message})
    return messages


def worker_task(client, request_id, manager, content: str = 'random'):
    """
    后台工作线程：执行对话任务，记录耗时，并最终释放客户端。
    """
    start_time = time.time()
    try:
        if content == 'random':
            prompt = get_random_test_prompt()
            messages = simple_chat(prompt)
        elif content == 'complex':
            messages = MESSAGE
        else:
            messages = content

        print(f"\n[Request #{request_id}] 🚀 Assigned to {client.name}")

        response = client.chat(messages=messages)

        # 这里假设 response 结构，根据实际情况调整
        # 如果 response 是对象，可能需要 response.content 或 str(response)
        # content = str(response)[:100] + "..."  # 只打印前100个字符避免刷屏
        content = str(response)

    except Exception as e:
        print(f"\n[Request #{request_id}] ❌ Error with {client.name}: {e}")
        traceback.print_exc()
    finally:
        duration = time.time() - start_time
        print(f"[Request #{request_id}] ✅ Done by {client.name} in {duration:.2f}s. \n   Response: {content}\n")

        # 【关键】任务结束后必须释放客户端
        manager.release_client(client)


def print_wait_status(count):
    """
    在同一行打印等待次数。
    \r : 回到行首
    \033[K : 清除光标后的内容 (可选，用于防止字符残留)
    """
    msg = f"\r⏳ No clients available. Waiting... (Attempts: {count})"
    sys.stdout.write(msg)
    sys.stdout.flush()


def setup_client_manager():
    client_manager = AIClientManager()
    clients = AI_CLIENTS

    # Register all clients.
    [client_manager.register_client(client) for client in clients.values()]

    # Or register specified client.
    # specified_client = clients['gemini']
    # client_manager.register_client(specified_client)
    # print(specified_client.get_model_list())

    # Set client group limit. Depends on AI service vendor's limitation.
    [client_manager.set_group_limit(group, limit) for group, limit in AI_CLIENT_LIMIT.items()]

    return client_manager


def run_ai_test(client_manager: AIClientManager):
    STATS_INTERVAL = 10  # 每处理多少个请求打印一次统计
    MAX_WORKERS = 5  # 线程池大小（最大并发数）

    request_counter = 0
    wait_loop_counter = 0
    is_waiting = False  # 标记当前是否处于“等待打印模式”

    print(f"Starting Load Test (Stats every {STATS_INTERVAL} requests)...")
    print("-" * 50)

    # 使用线程池来处理并发请求
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while True:
            # 尝试获取客户端
            request_counter += 1
            client = client_manager.get_available_client(f'AI Client Usage Demo ({request_counter})')

            if not client:
                # --- Case A: 没有可用客户端 ---
                wait_loop_counter += 1
                is_waiting = True
                print_wait_status(wait_loop_counter)
                time.sleep(0.5)  # 等待间隔
                continue

            # --- Case B: 获取到客户端 ---

            # 1. 如果之前在打印等待条，先换行，避免被覆盖
            if is_waiting:
                sys.stdout.write("\n")  # 结束那一行等待提示
                is_waiting = False
                wait_loop_counter = 0

            request_counter += 1

            # 2. 异步提交任务
            # 注意：不要在这里做耗时的 chat 操作，否则 while 循环会卡住
            executor.submit(worker_task, client, request_counter, client_manager)

            # 3. 定期打印统计信息
            if request_counter % STATS_INTERVAL == 0:
                # 稍微延迟一下打印，防止和上面的 submit 里的 print 混在一起
                time.sleep(0.1)
                print("\n" + "=" * 20 + f" STATS REPORT (Req #{request_counter}) " + "=" * 20)
                stats = client_manager.get_client_stats()

                stats_str = client_manager.format_stats_report(stats)
                print(stats_str)

            # if request_counter >= 260:
            #     break

            # 稍微 sleep 一下避免 CPU 空转太快（如果有大量客户端，这个可以设很小）
            time.sleep(0.1)


def main():
    setup_colored_logging()

    client_manager = setup_client_manager()
    client_manager.start_monitoring()

    client_backend = AIDashboardService(client_manager)
    threading.Thread(target=client_backend.run_standalone).start()

    run_ai_test(client_manager)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nTest stopped by user.")
    except Exception as e:
        print(str(e))
        print(traceback.format_exc())
