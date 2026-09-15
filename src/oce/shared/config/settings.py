"""应用配置。每个配置组使用独立环境变量前缀。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """数据库配置（PostgreSQL / SQLite 元数据存储）"""

    model_config = SettingsConfigDict(
        env_prefix="DB_",
        env_file=[".env", ".env.local"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = Field(
        default="postgresql+asyncpg://oce:oce@localhost:5432/oce",
        description="数据库连接 URL"
    )
    pool_size: int = Field(default=5, ge=1, le=100, description="连接池大小")
    max_overflow: int = Field(default=5, ge=0, le=100, description="连接池溢出上限")
    echo: bool = Field(default=False, description="是否打印 SQL 日志")

    @property
    def is_sqlite(self) -> bool:
        """是否是 SQLite"""
        return self.url.startswith("sqlite")


class MilvusSettings(BaseSettings):
    """Milvus 3.0 配置（向量存储 + 混合检索）"""

    model_config = SettingsConfigDict(
        env_prefix="MILVUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 连接
    endpoint: str = Field(
        default="http://localhost:19530",
        description="Milvus 端点：HTTP 服务地址，或本地 Milvus Lite 文件路径",
    )
    token: str | None = Field(default=None, description="认证 token（Zilliz Cloud）")

    # Collection
    collection_name: str = Field(default="oce_chunks", description="Collection 名称")
    path_collection_name: str = Field(
        default="oce_paths_v1",
        description="路径索引 Collection 名称",
    )
    dense_dim: int = Field(default=1024, description="密集向量维度")

    # 索引
    dense_index_type: str = Field(default="HNSW", description="密集向量索引类型")
    dense_metric_type: str = Field(default="COSINE", description="密集向量距离度量")

    # HNSW 参数
    hnsw_m: int = Field(default=16, ge=4, le=64, description="HNSW M 参数")
    hnsw_ef_construction: int = Field(default=256, ge=8, le=512, description="HNSW efConstruction")
    hnsw_ef_search: int = Field(default=64, ge=8, le=2048, description="HNSW ef（搜索时）")


class EmbeddingSettings(BaseSettings):
    """嵌入模型配置"""

    model_config = SettingsConfigDict(
        env_prefix="EMBED_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=True, description="是否启用嵌入(关闭时只切块不嵌入)")
    endpoint: str = Field(
        default="https://api.siliconflow.cn/v1/embeddings",
        description="OpenAI 兼容的 embedding 端点",
    )
    api_key: SecretStr | None = Field(default=None, description="Embedding API 密钥")
    model: str = Field(default="Qwen/Qwen3-Embedding-4B", description="嵌入模型")
    dimensions: int = Field(default=1024, ge=1, description="向量维度")
    max_batch_size: int = Field(default=32, ge=1, le=256, description="单请求文本数")
    max_batch_chars: int = Field(
        default=32_000,
        ge=1,
        description="单请求 input 数组总字符预算",
    )
    max_input_chars: int = Field(default=8_000, ge=1, description="单条模型输入字符上限")
    input_overlap_chars: int = Field(default=400, ge=0, description="长输入分段重叠字符数")
    max_concurrency: int = Field(default=4, ge=1, le=32, description="最大请求并发")
    timeout_seconds: float = Field(default=60.0, gt=0, description="请求超时秒数")
    proxy: str | None = Field(default=None, description="可选 HTTP 代理")
    query_instruction: str = Field(
        default="",
        description="Query-side instruction（添加到 query 前，为空则不添加）",
    )


class RerankSettings(BaseSettings):
    """重排模型配置。"""

    model_config = SettingsConfigDict(
        env_prefix="RERANK_",
        env_file=[".env", ".env.local"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, description="是否启用 API 重排（已被 LLM 重排取代，默认关）")
    endpoint: str = Field(
        default="https://api.siliconflow.cn/v1/rerank",
        description="Rerank 端点",
    )
    api_key: SecretStr | None = Field(default=None, description="空值时复用 embedding key")
    model: str = Field(default="Qwen/Qwen3-Reranker-0.6B", description="重排模型")
    top_n: int = Field(default=10, ge=1, le=100, description="重排返回数")
    min_score: float = Field(default=0.05, ge=0.0, le=1.0, description="最低重排分")
    timeout_seconds: float = Field(default=60.0, gt=0, description="请求超时秒数")


class LLMSettings(BaseSettings):
    """共享 LLM 客户端配置。

    被三个功能共用同一个 OpenAI 兼容 client：LLM 语义重排（rerank_enabled）、
    查询改写（RetrievalSettings.query_rewrite_enabled）、意图分类
    （RetrievalSettings.intent_classification_enabled）。三者任一开启即初始化。
    """

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=[".env", ".env.local"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    rerank_enabled: bool = Field(default=True, description="是否启用 LLM 语义重排（默认主重排层）")
    model: str = Field(default="Qwen/Qwen2.5-7B-Instruct", description="LLM 模型")
    api_key: SecretStr = Field(default="", description="LLM API Key")
    base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="LLM API Base URL",
    )
    proxy: str | None = Field(default=None, description="LLM API HTTP 代理")
    max_candidates: int = Field(default=50, ge=10, le=100, description="LLM 重排最大候选数")
    output_top_k: int = Field(default=10, ge=1, le=50, description="LLM 重排输出数")
    # 实测 chunk 中位长度约 1560 字符，99% 超过 400；截断过短会让 LLM 只看到片段开头
    snippet_chars: int = Field(
        default=1600, ge=200, le=4000, description="每个候选送入 LLM 的代码字符上限"
    )
    # 单次 rerank 可达 16k token，不限流会在十几个查询后连续 429 并静默退回原始顺序
    tpm_limit: int = Field(
        default=60_000, ge=1_000, description="LLM 接口 TPM 上限，0 以上时客户端排队"
    )


class RetrievalSettings(BaseSettings):
    """检索配置"""

    model_config = SettingsConfigDict(
        env_prefix="RETRIEVAL_",
        env_file=[".env", ".env.local"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 向量检索
    default_top_k: int = Field(default=50, ge=1, le=200, description="向量召回条数")
    vector_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Milvus dense 相似度过滤阈值；默认不预过滤",
    )
    final_select_k: int = Field(default=10, ge=1, le=50, description="最终返回条数")

    # 多查询融合
    rrf_k: int = Field(default=60, ge=1, description="多查询结果融合平滑常数")

    # 置信度门槛
    confidence_floor: float = Field(default=0.0, ge=0.0, le=1.0, description="最终置信度门槛")

    # 精确标识符召回
    exact_max_scope_blobs: int = Field(
        default=2_000,
        ge=0,
        description="SQL 精确标识符召回允许的最大 blob scope；0 表示禁用",
    )
    exact_timeout_seconds: float = Field(
        default=2.0,
        gt=0.0,
        description="SQL 精确标识符召回超时；超时后回退向量检索",
    )

    # 仓库级多意图召回
    query_decomposition_enabled: bool = Field(default=True, description="是否分解多句检索请求")
    query_max_queries: int = Field(default=4, ge=1, le=8, description="原查询和子查询总数上限")
    query_min_facet_chars: int = Field(default=8, ge=1, description="子查询最少字符数")
    query_facet_weight: float = Field(default=0.75, gt=0.0, le=1.0, description="子查询融合权重")
    per_query_top_k: int = Field(default=20, ge=1, le=100, description="单查询模式下覆盖 default_top_k")

    # 上下文剪枝与覆盖度（字符预算为硬限制，final_select_k 为软上限）
    max_chunks_per_path: int = Field(default=2, ge=1, le=20, description="单文件最多返回片段数")
    max_context_chars: int = Field(default=32_000, ge=1, description="返回代码总字符预算（硬限制）")
    overlap_threshold: float = Field(default=0.6, ge=0.0, le=1.0, description="同文件片段重叠抑制阈值")

    # Query rewrite (LLM-based query expansion for better recall)
    # 默认关闭：仅跨语言文件名等特殊场景有明显增益，通用检索收益有限
    query_rewrite_enabled: bool = Field(default=False, description="是否启用 LLM 查询改写")
    query_rewrite_model: str = Field(default="Qwen/Qwen2.5-7B-Instruct", description="查询改写使用的 LLM 模型")
    query_rewrite_num: int = Field(default=3, ge=1, le=5, description="生成改写查询的数量")

    # Path index (独立路径索引用于文件名查询)
    path_index_enabled: bool = Field(default=True, description="是否启用路径索引（文件名查询增强）")
    # 路径分数与内容分数同为 COSINE 量纲，加权相加而非替换，避免挤掉正确 chunk
    path_boost_weight: float = Field(
        default=0.5, ge=0.0, le=2.0, description="路径索引命中对同文件 chunk 的加权系数"
    )

    # Intent classification (意图分类驱动的检索策略)
    intent_classification_enabled: bool = Field(default=True, description="是否启用查询意图分类（LLM-based）")


class RedisSettings(BaseSettings):
    """Redis 配置（任务队列）"""

    model_config = SettingsConfigDict(
        env_prefix="REDIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = Field(default="redis://localhost:6379/0", description="Redis 连接 URL")
    queue_name: str = Field(default="oce:embed_queue", description="嵌入队列名称")


class WorkerSettings(BaseSettings):
    """Worker 配置（后台嵌入消费者）"""

    model_config = SettingsConfigDict(
        env_prefix="WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=True, description="是否启用后台 worker")
    concurrency: int = Field(default=2, ge=1, le=32, description="并发消费协程数")
    max_retries: int = Field(default=3, ge=1, le=10, description="失败重试上限")


class LogSettings(BaseSettings):
    """日志配置"""

    model_config = SettingsConfigDict(
        env_prefix="LOG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    file_enabled: bool = Field(default=False, description="是否启用日志落盘")
    file_path: str | None = Field(default=None, description="日志文件路径（None 时自动推断）")
    rotation: str = Field(default="100 MB", description="轮转策略：'1 day' 按天 / '100 MB' 按大小")
    retention: str = Field(default="30 days", description="保留时长：'30 days' / '10 files'")
    format_json: bool = Field(default=False, description="是否使用 JSON 格式（便于日志采集）")
    level: str = Field(default="INFO", description="日志级别（WARNING/INFO/DEBUG）")


class MonitoringSettings(BaseSettings):
    """监控配置（调用 / token / 资源采集与落库）"""

    model_config = SettingsConfigDict(
        env_prefix="MONITORING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=True, description="是否启用监控采集与落库")
    flush_interval_seconds: float = Field(
        default=5.0, gt=0, description="缓冲区批量写库间隔秒数"
    )
    flush_max_buffer: int = Field(
        default=500, ge=1, description="单类指标缓冲上限，超出立即 flush"
    )
    resource_sample_interval_seconds: float = Field(
        default=60.0, gt=0, description="资源采样间隔秒数"
    )
    retention_days: int = Field(
        default=30, ge=1, description="监控数据保留天数（GC 清理阈值）"
    )
    cleanup_interval_seconds: float = Field(
        default=3600.0, gt=0, description="监控数据清理任务运行间隔秒数"
    )
    retrieval_audit_enabled: bool = Field(
        default=True, description="是否记录检索各阶段耗时与空回审计"
    )
    store_query_text: bool = Field(
        default=False, description="检索审计是否存储 query 原文（默认关，隐私安全）"
    )


class AuthSettings(BaseSettings):
    """多用户接入配置（LinuxDo OAuth2 + 每用户 API key）。

    默认整体关闭：不配置任何 AUTH_* 时行为与单 key 模式逐字节一致（个人模式安全）。
    """

    model_config = SettingsConfigDict(
        env_prefix="AUTH_",
        env_file=[".env", ".env.local"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, description="是否启用多用户接入")
    client_id: str = Field(default="", description="LinuxDo Connect 应用 client_id")
    client_secret: SecretStr = Field(default=SecretStr(""), description="LinuxDo Connect 应用 client_secret")
    # redirect_uri 单一真源：authorize 与 token 请求及 connect.linux.do 注册必须逐字节一致
    redirect_base: str = Field(
        default="",
        description="对外基地址（如 https://oce.melikeme.cn），回调 = base + /auth/callback",
    )
    redirect_uri_override: str | None = Field(
        default=None, description="显式覆盖完整回调 URL（优先于 redirect_base）"
    )
    authorize_url: str = Field(
        default="https://connect.linux.do/oauth2/authorize", description="OAuth2 授权端点"
    )
    token_url: str = Field(
        default="https://connect.linux.do/oauth2/token", description="OAuth2 令牌端点"
    )
    userinfo_url: str = Field(
        default="https://connect.linux.do/oauth2/userinfo",
        description="用户信息端点（社区文档存在 /api/user 与 /oauth2/userinfo 两种，可覆盖）",
    )
    session_secret: SecretStr = Field(
        default=SecretStr(""), description="会话 cookie 签名密钥；轮换会使全部会话失效"
    )
    session_ttl_seconds: int = Field(default=7 * 24 * 3600, gt=0, description="会话有效期")
    state_ttl_seconds: int = Field(default=600, gt=0, description="OAuth state 有效期（秒）")
    cookie_secure: bool = Field(
        default=False,
        description="会话 cookie 是否加 Secure；nginx TLS 后置 true（直连 http 会丢 cookie）",
    )
    min_trust_level: int | None = Field(
        default=None,
        description="可选信任等级门槛；None=不限制（准入交给 connect.linux.do 应用设置）",
    )
    portal_dist_dir: str = Field(
        default="", description="门户前端构建产物目录（容器内如 /app/portal-dist）；空则不挂载"
    )
    max_users: int = Field(
        default=0,
        ge=0,
        description="注册名额上限（0=不限）；可在 admin 面板运行时覆盖",
    )

    @property
    def redirect_uri(self) -> str:
        if self.redirect_uri_override:
            return self.redirect_uri_override
        return self.redirect_base.rstrip("/") + "/auth/callback"

    @model_validator(mode="after")
    def _require_credentials_when_enabled(self) -> "AuthSettings":
        # 启用即缺必填 → 启动期失败，而不是第一次登录才发现
        if self.enabled:
            missing = [
                name
                for name, value in (
                    ("AUTH_CLIENT_ID", self.client_id),
                    ("AUTH_CLIENT_SECRET", self.client_secret.get_secret_value()),
                    ("AUTH_SESSION_SECRET", self.session_secret.get_secret_value()),
                    # 校验源头而非派生的 redirect_uri：base 为空时派生值 "/auth/callback"
                    # 非空但无意义
                    ("AUTH_REDIRECT_BASE 或 AUTH_REDIRECT_URI", self.redirect_uri_override or self.redirect_base),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"AUTH_ENABLED=true 但缺少必填配置: {', '.join(missing)}")
        return self


class Settings(BaseSettings):
    """全局配置 - 聚合所有配置组"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # API
    api_key: str = Field(
        default="sk-opencontextengine",
        description="API 认证密钥；个人模式用与客户端约定的固定值，服务模式须改为强随机值",
    )
    admin_api_key: str = Field(
        default="",
        description="Admin 接口密钥；空则回落 API_KEY，一旦配置则 admin 接口只认此 key",
    )
    cors_origins: str = Field(
        default="https://oce-ai.github.io",
        description="允许访问 API 的浏览器来源，多个来源用逗号分隔；默认放行官方 admin 面板，设为空则关闭 CORS",
    )

    # 子配置组
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    milvus: MilvusSettings = Field(default_factory=MilvusSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例（缓存）"""
    return Settings()
