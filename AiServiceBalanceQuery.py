import aiohttp
import asyncio
import logging
import traceback
from typing import Dict, Any


logger = logging.getLogger(__name__)


SILICONFLOW_API_URL = "https://api.siliconflow.cn/v1/user/info"
OPENAI_API_BASE_URL = "https://api.openai.com"
DEEPSEEK_API_URL = "https://api.deepseek.com/user/balance"


class BalanceQueryService:
    """
    A unified service class for querying balance information from various AI platforms.
    Returns structured data instead of formatted strings for easy programmatic consumption.
    """

    def __init__(self, request_timeout: float = 10.0):
        """
        Initialize the balance query service.

        Args:
            request_timeout: Timeout for API requests in seconds
        """
        self.request_timeout = request_timeout
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.request_timeout))
        return self._session

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def query_siliconflow(self, api_key: str) -> Dict[str, Any]:
        """
        Query SiliconFlow platform balance information.

        Args:
            api_key: SiliconFlow API key

        Returns:
            Dictionary containing balance information with structured data
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        result = {
            "success": False,
            "platform": "siliconflow",
            "data": {},
            "error": None
        }

        try:
            session = await self._get_session()
            async with session.get(SILICONFLOW_API_URL, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()

                if data.get('status') and data.get('data'):
                    balance_info = data['data']
                    result.update({
                        "success": True,
                        "data": {
                            "user_id": balance_info.get('id'),
                            "username": balance_info.get('name'),
                            "email": balance_info.get('email'),
                            "balance_usd": balance_info.get('balance'),
                            "charge_balance_usd": balance_info.get('chargeBalance'),
                            "total_balance_usd": balance_info.get('totalBalance')
                        }
                    })
                else:
                    result["error"] = data.get('message', 'Unknown error')

        except aiohttp.ClientError as e:
            result["error"] = f"Request error: {str(e)}"
        except Exception as e:
            result["error"] = f"Unexpected error: {str(e)}"

        return result

    async def query_openai(self, api_key: str) -> Dict[str, Any]:
        """
        Query OpenAI platform balance information.

        Args:
            api_key: OpenAI API key

        Returns:
            Dictionary containing balance information with structured data
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        result = {
            "success": False,
            "platform": "openai",
            "data": {},
            "error": None
        }

        try:
            from datetime import datetime
            today = datetime.today().strftime('%Y-%m-%d')

            session = await self._get_session()

            # Get subscription information
            subscription_url = f"{OPENAI_API_BASE_URL}/v1/dashboard/billing/subscription"
            async with session.get(subscription_url, headers=headers) as subscription_response:
                subscription_response.raise_for_status()
                subscription_data = await subscription_response.json()

            # Get usage information
            usage_url = f"{OPENAI_API_BASE_URL}/v1/dashboard/billing/usage?start_date={today}&end_date={today}"
            async with session.get(usage_url, headers=headers) as usage_response:
                usage_response.raise_for_status()
                usage_data = await usage_response.json()

            account_balance = subscription_data.get("soft_limit_usd", 0)
            used_balance = usage_data.get("total_usage", 0) / 100
            remaining_balance = account_balance - used_balance

            result.update({
                "success": True,
                "data": {
                    "has_payment_method": subscription_data.get('has_payment_method', False),
                    "account_balance_usd": round(account_balance, 2),
                    "used_balance_usd": round(used_balance, 2),
                    "remaining_balance_usd": round(remaining_balance, 2),
                    "access_until": subscription_data.get('access_until'),
                    "soft_limit_usd": subscription_data.get('soft_limit_usd'),
                    "hard_limit_usd": subscription_data.get('hard_limit_usd')
                }
            })

        except aiohttp.ClientError as e:
            result["error"] = f"Request error: {str(e)}"
        except Exception as e:
            result["error"] = f"Unexpected error: {str(e)}"

        return result

    async def query_deepseek(self, api_key: str) -> Dict[str, Any]:
        """
        Query DeepSeek platform balance information.

        Args:
            api_key: DeepSeek API key

        Returns:
            Dictionary containing balance information with structured data
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json"
        }

        result = {
            "success": False,
            "platform": "deepseek",
            "data": {},
            "error": None
        }

        try:
            session = await self._get_session()
            async with session.get(DEEPSEEK_API_URL, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()

                if data.get('is_available') is False:
                    result["error"] = "Account unavailable or no balance information (not topped up)"
                    return result

                if data.get('balance_infos') and len(data['balance_infos']) > 0:
                    balance_info = data['balance_infos'][0]
                    result.update({
                        "success": True,
                        "data": {
                            "currency": balance_info.get('currency'),
                            "total_balance": balance_info.get('total_balance'),
                            "granted_balance": balance_info.get('granted_balance'),
                            "topped_up_balance": balance_info.get('topped_up_balance'),
                            "is_available": data.get('is_available', True)
                        }
                    })
                else:
                    result["error"] = "No balance information found in response"

        except aiohttp.ClientError as e:
            result["error"] = f"Request error: {str(e)}"
        except Exception as e:
            result["error"] = f"Unexpected error: {str(e)}"

        return result

    async def query_all_platforms(self, api_keys: Dict[str, str]) -> Dict[str, Any]:
        """
        Query balance information from all platforms simultaneously.

        Args:
            api_keys: Dictionary mapping platform names to API keys
                    Example: {'siliconflow': 'key1', 'openai': 'key2', 'deepseek': 'key3'}

        Returns:
            Dictionary containing results from all platforms
        """
        import asyncio

        tasks = []

        if 'siliconflow' in api_keys:
            tasks.append(self.query_siliconflow(api_keys['siliconflow']))

        if 'openai' in api_keys:
            tasks.append(self.query_openai(api_keys['openai']))

        if 'deepseek' in api_keys:
            tasks.append(self.query_deepseek(api_keys['deepseek']))

        if not tasks:
            return {
                "success": False,
                "error": "No valid API keys provided",
                "results": {}
            }

        # Execute all queries concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        formatted_results = {}
        for result in results:
            if isinstance(result, Exception):
                formatted_results["error"] = f"Exception occurred: {str(result)}"
            else:
                platform = result.get("platform")
                formatted_results[platform] = result

        return {
            "success": any(r.get("success", False) for r in formatted_results.values()
                           if isinstance(r, dict) and "success" in r),
            "results": formatted_results
        }


# --------------------------------- Easy for use sync functions ---------------------------------

def get_siliconflow_balance(api_key: str) -> dict:
    """
    Synchronous compatibility function that maintains the original interface.
    Returns formatted string for backward compatibility.
    """
    async def _async_query():
        service = BalanceQueryService()
        try:
            result = await service.query_siliconflow(api_key)
            return result
        except Exception as e:
            logger.error(str(e))
            traceback.print_exc()
            return {}
        finally:
            await service.close()
    return asyncio.run(_async_query())


def get_openai_balance(api_key: str) -> dict:
    """
    Synchronous compatibility function that maintains the original interface.
    Returns formatted string for backward compatibility.
    """
    async def _async_query():
        service = BalanceQueryService()
        try:
            result = await service.query_openai(api_key)
            return result
        except Exception as e:
            logger.error(str(e))
            traceback.print_exc()
            return {}
        finally:
            await service.close()
    return asyncio.run(_async_query())


def get_ds_balance(api_key: str) -> dict:
    """
    Synchronous compatibility function that maintains the original interface.
    Returns formatted string for backward compatibility.
    """
    async def _async_query():
        service = BalanceQueryService()
        try:
            result = await service.query_deepseek(api_key)
            return result
        except Exception as e:
            logger.error(str(e))
            traceback.print_exc()
            return {}
        finally:
            await service.close()
    return asyncio.run(_async_query())


def _format_result_to_string(result: Dict[str, Any]) -> str:
    """
    Convert structured result to human-readable string for compatibility.
    """
    if not result.get("success"):
        return f"Query failed: {result.get('error', 'Unknown error')}"

    platform = result.get("platform", "unknown")
    data = result.get("data", {})

    if platform == "siliconflow":
        return (
            f"硅基流动账户余额信息:\n"
            f"用户ID: {data.get('user_id', 'N/A')}\n"
            f"用户名: {data.get('username', 'N/A')}\n"
            f"邮箱: {data.get('email', 'N/A')}\n"
            f"余额: {data.get('balance_usd', 'N/A')}\n"
            f"充值余额: {data.get('charge_balance_usd', 'N/A')}\n"
            f"总余额: {data.get('total_balance_usd', 'N/A')}\n"
        )
    elif platform == "openai":
        return (
            f"OpenAI账户余额信息:\n"
            f"是否已绑定支付方式: {'是' if data.get('has_payment_method') else '否'}\n"
            f"账户额度: {data.get('account_balance_usd', 0):.2f}\n"
            f"已使用额度: {data.get('used_balance_usd', 0):.2f}\n"
            f"剩余额度: {data.get('remaining_balance_usd', 0):.2f}\n"
            f"API访问权限截止时间: {data.get('access_until', '无限制')}\n"
        )
    elif platform == "deepseek":
        return (
            f"DeepSeek账户余额信息:\n"
            f"币种: {data.get('currency', 'N/A')}\n"
            f"总余额: {data.get('total_balance', 'N/A')}\n"
            f"已授予余额: {data.get('granted_balance', 'N/A')}\n"
            f"充值余额: {data.get('topped_up_balance', 'N/A')}\n"
        )
    else:
        return f"Unknown platform: {platform}"


# ----------------------------------------------------------------------------------------------------------------------

def main():
    with open('C:\D\Code\git\IntelligenceIntegrationSystem\siliconflow_keys.txt', 'rt') as f:
        while True:
            line = f.readline().strip()
            if not line:
                break
            result = get_siliconflow_balance(line)
            print(_format_result_to_string(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(str(e))
        print(traceback.format_exc())
