from __future__ import annotations

"""
数据库连接配置（独立分组）。

只放 PostgreSQL 连接 4 元组（host / port / db / user / password）。
不放任何业务参数——这样未来切 DB / 多环境时改这一个文件就够。

被 `db/connection.py` 直接读取，是新老链路共享的基础设施。
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DatabaseSettings:
    # DB 所在主机。可以是域名、IP 或容器名。
    # 优先读环境变量 DB_HOST（容器部署用 poms-postgres 容器名互联），
    # DB 主机。Docker 部署时默认用容器名 "poms-postgres";
    db_host: str = field(
        default_factory=lambda: os.environ.get("DB_HOST", "poms-postgres")
    )
    # PostgreSQL 端口，默认 5432。
    db_port: int = 5432
    # 业务库名，应与上游 API 服务使用同一个库（共享原始表）。
    db_name: str = "all_new"
    # 连接用户名。最小权限需要：SELECT / UPDATE 原始表 + 全权限 summary_level1 / summary_level2。
    db_user: str = "all_new"
    # 连接密码。生产环境建议改成从环境变量或密钥管理系统读取，当前版本为了简单直接写明文。
    db_password: str = "123qwe"
