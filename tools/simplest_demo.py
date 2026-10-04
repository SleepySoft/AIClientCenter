import os

from AIClientCenter.providers.openai_clients import StandardOpenAIClient
from AIClientCenter.providers.openai_compatible import OpenAICompatibleAPI


api = OpenAICompatibleAPI(
    api_base_url='https://integrate.api.nvidia.com/v1',
    token=os.getenv("NVIDIA_API_KEY"),
    default_model='moonshotai/kimi-k2.5'
)

client = StandardOpenAIClient(name='Demo Client', openai_api=api)

messages = [
    {"role": "system", "content": 'You are a assistant.'},
    {"role": "user", "content": '你是什么模型？'}]


response = client.chat(messages)

print(response)
