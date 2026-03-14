# 情绪/状态衰减参数
TIME_TO_FULL_LONELINESS: float = 60.0 * 60 * 3  # 孤独感从 0 涨满所需秒数（3 小时）
TIME_TO_FULL_BOREDOM: float = 60.0 * 60 * 2  # 无聊感从 0 涨满所需秒数（2 小时）

LONELINESS_RATE: float = 1.0 / TIME_TO_FULL_LONELINESS  # 每秒孤独感增长率
BOREDOM_RATE: float = 1.0 / TIME_TO_FULL_BOREDOM  # 每秒无聊感增长率

# 主循环行为参数
CURIOSITY_THRESHOLD: float = 0.6  # 好奇心超过此值时主动发起对话
SESSION_IDLE_TIMEOUT: float = 60.0 * 30  # 会话无交互超时时长（秒，30 分钟）

# Butler Agent 参数
MAX_BUTLER_LOOPS: int = 3  # 管家 Agent 单次命令最大内循环次数

# Digest Agent 参数
DIGEST_MIN_SCORE: int = 55  # 话题评估最低分，低于该值不进入主动话题队列
DIGEST_INTERVAL_SECONDS: float = 60.0 * 60 * 2  # 后台 Digest 执行间隔（2 小时）
DIGEST_STARTUP_DELAY: float = 60.0  # 启动后首次 Digest 执行延迟（秒）

# 主动发言（孤独驱动）行为参数
PROACTIVE_COOLDOWN: float = 60.0 * 60 * 1  # 两次主动情绪发言之间的最小间隔（1 小时）。
# 发言后孤独感不会清零，但此冷却期保证她不会连续倾诉。
LONELINESS_PROACTIVE_RELIEF: float = 0.35
# 主动发言后孤独感降低的幅度。表达出来会有所缓解，但不等于彻底不孤独。
