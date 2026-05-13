#!/bin/bash
# 安全重启 main.py 服务。
#
# 用法：
#   ./scripts/restart.sh           # 前台跑（看日志方便，Ctrl+C 退出）
#   ./scripts/restart.sh --bg      # 后台跑（关 Terminal 也不停）
#
# 流程：
# 1. 找出所有 main.py 进程 → 优雅 SIGINT 退出
# 2. 等最多 15 秒确认全退（worker shutdown 默认 10s）
# 3. 启动新进程
#
# 为什么需要这个：
# 直接 `python main.py` 启动新进程不会停旧的，导致两个进程同时写 DB，
# UPSERT 会让"老进程的过期写入"覆盖"新进程的正确写入"，
# 表现为"我改了配置怎么没生效"——这是 hotness_exclude_entities 那次踩过的坑。

set -e

cd "$(dirname "$0")/.."   # 切到项目根目录

# ---------- Step 1：优雅停掉所有 main.py ----------
PIDS=$(pgrep -f "python.*main.py" || true)
if [ -n "$PIDS" ]; then
    echo "[restart] 发现运行中的 main.py 进程：$PIDS"
    echo "[restart] 发送 SIGINT，等待优雅退出..."
    # shellcheck disable=SC2086
    kill -INT $PIDS

    # 等最多 15 秒
    for i in {1..15}; do
        REMAINING=$(pgrep -f "python.*main.py" || true)
        if [ -z "$REMAINING" ]; then
            echo "[restart] 所有旧进程已退出（用时 ${i}s）"
            break
        fi
        sleep 1
    done

    # 仍有残留 → 强杀
    REMAINING=$(pgrep -f "python.*main.py" || true)
    if [ -n "$REMAINING" ]; then
        echo "[restart] ⚠️ 15 秒后仍有进程残留：$REMAINING，发送 SIGKILL"
        # shellcheck disable=SC2086
        kill -9 $REMAINING
        sleep 2
    fi
else
    echo "[restart] 无旧进程，直接启动"
fi

# ---------- Step 2：启动新进程 ----------
if [ "$1" = "--bg" ]; then
    echo "[restart] 后台启动（日志见 logs/service.log）"
    nohup .venv/bin/python main.py > /dev/null 2>&1 &
    NEW_PID=$!
    sleep 2
    if ps -p "$NEW_PID" > /dev/null; then
        echo "[restart] ✅ 启动成功，PID=$NEW_PID"
        echo "[restart] 看日志：tail -f logs/service.log"
    else
        echo "[restart] ❌ 启动失败，查 logs/service.log"
        exit 1
    fi
else
    echo "[restart] 前台启动（Ctrl+C 退出）"
    exec .venv/bin/python main.py
fi
