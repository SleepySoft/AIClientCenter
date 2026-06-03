import os

from AIClientCenter.AIClients import StandardOpenAIClient
from AIClientCenter.OpenAICompatibleAPI import OpenAICompatibleAPI


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
