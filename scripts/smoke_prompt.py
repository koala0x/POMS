"""
端到端 prompt 冒烟脚本:不依赖数据库,直接构造样本数据 → 渲染 prompt → 调 LLM。

用法:
    ./.venv/bin/python scripts/smoke_prompt.py [level1|level2]

"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import get_settings
from llm.ollama_client import OllamaClient


SAMPLE_POSTS_LEVEL1 = [
    # 加密主流币
    ("@WhaleAlert", "BlackRock IBIT 单日净流入 $2.31B,创今年新高,机构 ETF 持仓占比突破 26%"),
    ("@CryptoQuant", "BTC 突破 $73,500,链上活跃地址 24h 增长 8.4%,Coinbase Premium 转正"),
    ("@VitalikButerin", "讨论 PoS 经济模型时,关键不是 yield,而是 slashing 与去中心化的平衡"),
    # meme / 土狗
    ("@SolanaMemeGuy", "$WIF 单日 +45%,SOL 链上 24h 交易量暴涨至 $5.2B,FOMO 情绪显著"),
    ("@PEPEDegen", "PEPE 这波快上车了铁子们,基本面我也不知道但社区在拉,反正涨就完了"),
    # 美股
    ("@CNBC", "苹果 Q2 财报营收 $94.5B 超预期,服务业务同比 +14%,盘后涨 6%"),
    ("@Reuters", "特斯拉宣布裁员 10%,股价盘前下跌 4.2%,马斯克称为下阶段增长做准备"),
    # AI
    ("@OpenAI", "GPT-5 正式发布,上下文窗口扩展至 1M tokens,API 价格下降 30%"),
    ("@AnthropicAI", "Claude 4.7 在 SWE-bench 取得 78.3%,较上代提升 12 个百分点"),
    # 半导体
    ("@DigiTimes", "TSMC 公布 Q1 财报,营收同比 +40%,3nm 制程产能预订到 2026 Q3"),
    ("@SemiAnalysis", "Nvidia B200 量产爬坡顺利,Q4 出货预计 80 万颗,AMD MI325X 跟进"),
    # 宏观 / 监管
    ("@FedNews", "美联储 5 月会议维持利率不变 5.25%-5.50%,鲍威尔暗示年内或降息 1 次"),
    ("@SEC_News", "SEC 推迟以太坊现货 ETF 决议至 5 月底,市场预期偏空"),
    # 噪音 / 情绪
    ("@CryptoTwitter", "感觉这次不一样,牛市才刚刚开始 🚀"),
    ("@RandomTrader", "刚刚梭哈了,要么富要么穷,赌一把"),
]


SAMPLE_LEVEL1_SUMMARIES = [
    """## 加密主流币
- 🔴 BlackRock IBIT 单日净流入 $2.31B,创今年新高
- 🟡 BTC 突破 $73,500,链上活跃地址 24h +8.4%
- ⚪ Vitalik 谈 PoS 经济模型,强调 slashing 与去中心化平衡

## 半导体
- 🔴 TSMC Q1 营收 +40%,3nm 产能预订到 2026 Q3
""",
    """## 加密主流币
- 🟡 BTC 站稳 $73K,Coinbase Premium 转正
- ⚪ 多家 KOL 看多年底突破 $80K

## meme / 土狗币
- 🟡 $WIF +45%,SOL 链上 24h 交易量 $5.2B

## 半导体
- 🔴 Nvidia B200 量产爬坡顺利,Q4 预计出货 80 万颗
""",
    """## 美股
- 🔴 苹果 Q2 营收 $94.5B 超预期,盘后 +6%
- 🔴 特斯拉裁员 10%,盘前 -4.2%

## AI / 大模型
- 🔴 GPT-5 发布,上下文 1M tokens,API 降价 30%
- 🔴 Claude 4.7 SWE-bench 78.3%
""",
]


def render_level1_prompt(source: str = "twitter") -> str:
    """构造 level1 prompt(15 条样本)。"""
    template_path = ROOT / "prompts" / f"level1_{source}.txt"
    template = template_path.read_text(encoding="utf-8")

    items = []
    for idx, (author, content) in enumerate(SAMPLE_POSTS_LEVEL1, start=1):
        items.append(f"{idx}. {author}\n{content}")
    return template.format(items="\n\n".join(items))


def render_level2_prompt(source: str = "twitter") -> str:
    """构造 level2 prompt(3 条 level1 摘要样本)。"""
    template_path = ROOT / "prompts" / f"level2_{source}.txt"
    template = template_path.read_text(encoding="utf-8")

    items = []
    for idx, summary in enumerate(SAMPLE_LEVEL1_SUMMARIES, start=1):
        items.append(f"{idx}. {summary.strip()}")
    return template.format(items="\n\n".join(items))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "level1"
    if mode not in ("level1", "level2"):
        print("用法:python scripts/smoke_prompt.py [level1|level2]")
        sys.exit(1)

    settings = get_settings()

    # 冒烟脚本默认用 qwen3:8b 快速验证 prompt 是否合格;
    # 想跑生产模型可以临时改成 qwen3:30b。
    model_override = "qwen3:8b"
    client = OllamaClient(
        base_url=settings.ollama_base_url,
        model=model_override,
        timeout_seconds=600,
        retry_times=1,
        retry_delay_seconds=0,
        enable_thinking=False,  # 关闭 qwen3 推理链,直接给答案
    )

    prompt = render_level1_prompt() if mode == "level1" else render_level2_prompt()

    print(f"===== 渲染后的 {mode} prompt({len(prompt)} 字符) =====")
    print(prompt)
    print()
    print(f"===== 调用 {model_override}(think=False) =====")
    print()

    output = client.chat(prompt)

    print(f"===== LLM 输出({len(output)} 字符) =====")
    print(output)


if __name__ == "__main__":
    main()
