#### 核心实现

- 入口与调度：启动 30s 轮询 + 每小时整点任务： main.py 、 jobs.py
- 配置读取（.env）： settings.py 、示例： .env.example
- DB 连接池与仓储层： connection.py 、 repositories
- Ollama 调用（120s 超时 + 3 次重试 + 10s 间隔 + 空内容判定失败）： ollama_client.py
- 一次摘要/二次摘要业务流程（按 twitter / binance_square 独立处理）： level1_service.py 、 level2_service.py
- Prompt 模板文件化解耦： prompts
- SQL 迁移脚本（手动执行）： db/migrations
- 日志目录与忽略规则： logs/.gitkeep 、 .gitignore
- 最小化单测已补齐（需要安装依赖后才能跑）： tests



- 安装依赖：
- pip3 install -r requirements.txt
- 配置环境变量：
- 复制 .env.example 为 .env 并填好 Postgres/Ollama
- 执行迁移 SQL（按顺序手动跑）：
- 001_add_is_summarized_to_twitter.sql
- 002_add_is_summarized_to_binance.sql
- 003_create_summary_level1.sql
- 004_create_summary_level2.sql
- 启动服务：
- python3 main.py