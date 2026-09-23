"""异环 RAG —— 本地知识库问答与自动联网更新工具。

包内所有模块都不得在源码中硬编码任何 API Key；
密钥只能来自用户本机配置（DPAPI 加密存储）或开发期 .env（不参与打包）。
"""

__version__ = "1.0.0"
APP_NAME = "NTE-RAG"
APP_TITLE = "异环 RAG 知识助手"
