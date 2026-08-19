"""AI-Qwen-Max —— 面向 AMD Ryzen AI Max+ 395 (Strix Halo) 的高能效本地 Qwen 推理服务。

架构：
  CLI / web 静态界面 (HTTP:127.0.0.1:8080)
        │
        ▼
  ai_qwen_max (FastAPI 前端：会话管理 / OpenAI 兼容反代 / Max API)
        │ 子进程 + HTTP:127.0.0.1:8081
        ▼
  llama-server (vendor/llama.cpp = Ryzen-UMA-Vulkan-llama 引擎 ryzen-uma-vulkan 分支
  纯平台层 + 产品层 patch: K8V8 / SSD cache / MTP / checkpoint / retokenize)
"""

__version__ = "1.0.0"
