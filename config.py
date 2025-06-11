# ==== Pixiv 下载器配置 ====
# 账号配置
REFRESH_TOKEN = "lewUHGet10Zkfu7VEdY1BL9mT5CaJIcNnA4WATF9MIY"
USER_ID = "46999347"

# 路径配置
DOWNLOAD_DIR = r"D:\PixivDownloader"

# 网络配置
PROXY = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}

# 内容过滤
EXCLUDE_MANGA = True
EXCLUDE_TAGS = ['ホモ', 'ゲイ', 'ケモホモ', 'BL', 'ケモショタ', 'おにショタ']

# 输出配置
OUTPUT_FORMAT = "jpg"  # 可选：original/jpg/webp
QUALITY = 85               # 图片质量1-100（仅对jpg/webp有效）

# 高级配置（需手动修改）
RANKING_MAX_ITEMS = 100      # 榜单最大下载数量
FOLLOW_MAX_ITEMS = 100      # 关注最大下载数量
REQUEST_INTERVAL = 2         # 请求间隔(秒)

# API响应调试
DEBUG_API_RESPONSE = False 
