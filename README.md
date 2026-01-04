# AIClientCenter

An AI client center that supports token and model rotation

## 前言

如果你需要一个专业的AI客户端聚合与负载均衡工具，可以参考以下项目：

> https://github.com/songquanpeng/one-api

> https://github.com/enricoros/big-AGI

如果你想降低成本，诸如使用硅基流动14元token号以及每天白嫖2000次魔搭API，请往下看。

## 起因

由于我的 [IntelligenceIntegrationSystem (IIS)](https://github.com/SleepySoft/IntelligenceIntegrationSystem) 
项目需要消耗大量的Token，为了降低成本，我尝试了各种办法。比如咸鱼的14元token，比如魔搭每日2000额度。 
但事实证明，白嫖这碗饭并不是这么容易吃的：

> + 硅基流动的服务在更换Token后可能会有一段时间一直返回503错误，几个小时后会恢复。
> 
> + 魔搭明面上的限制是每日2000条，单个模型500次。实际上400B以上可用的模型只有3个，而且服务不稳定，且容易触发敏感词。

于是就诞生了这个项目，本项目主要实现我的以下需求：

1. 多Client管理，根据可用性及价格动态获取可用的使用价格最低Client，可以多线程多Client并行处理。
2. 支持Token及模型轮换，同时支持查询特定服务提供商的Token余额。
3. 自动用量统计，支持限额及余额两种模式，并判断客户端的健康度。
4. 自动管理Client的错误状态，尽可能及时发现出错的客户端，仅返回可用的客户端
5. 自动测试Client的可用性，及时发现已恢复的服务。

## 快速运行及预览

安装 [requirements.txt](requirements.txt) 后，
运行 [AIClientUsage.py](AIClientUsage.py) ，
并访问 [http://127.0.0.1:8000/](http://127.0.0.1:8000/) 查看管理页面。

## 说明

[AIClientUsage.py](AIClientUsage.py)

> Demo及示例代码，阅读该代码能够了解各个组件的使用，运行该代码可以测试当前环境的可用性。

[AIClientManager.py](AIClientManager.py)

> 核心代码：BaseAIClient 接口的定义及 Client 的管理。

[AIClientManagerBackend.py](AIClientManagerBackend.py)

> 网页管理工具后端，内联前端网页。该功能可选，完全可以不使用该模块，但建议使用。

[AIClients.py](AIClients.py)

> AI Client的实现，依赖于 OpenAICompatibleAPI ，并默认混入了 ClientMetricsMixin 。

[LimitMixins.py](LimitMixins.py)

> 余额及用量统计的“混入”类。混入该类以支持用量和健康度统计。

[AIServiceTokenRotator.py](AIServiceTokenRotator.py)

> 批量Token管理及自动轮转功能。

[SimpleRotator.py](SimpleRotator.py)

> 机械的轮换功能，仅通过计数进行轮换。用于像魔搭这种限制单个模型使用量的平台。

[AiServiceBalanceQuery.py](AiServiceBalanceQuery.py)

> 特定服务提供商的余额查询。

[OpenAICompatibleAPI.py](OpenAICompatibleAPI.py)

> OpenAI风格的API接口，通常并不会直接使用，而是将其传入 BaseAIClient 并加入 AIClientManager 统一管理。

[ZhipuSDKAdapter.py](ZhipuSDKAdapter.py)

> 智谱的客户端，使用官方API。智谱注册和实名认证后有一定免费额度。

[GoogleGeminiAdapter.py](GoogleGeminiAdapter.py)

> Gemini客户端。由于本人账号限制，无法白嫖。


---------

## 其它

各个平台的免费额度政策经常变，因此很可能出现不稳定或一段时间拒绝服务的情况。并且在使用时注意限制同一个平台的并发访问数量（AIClientManager.set_group_limit()）。

