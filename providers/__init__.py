"""服务商适配器及声明式配置预置。

底层 Client/API 负责与厂商通信；名称为 ``create_*_preset`` 的函数遵循
``factory_engine.ClientFactory`` 契约，可作为用户自定义配置工厂的参考实现。
"""
