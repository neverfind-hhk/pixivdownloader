#!/usr/bin/env python3
# coding=utf-8
' download module '
__author__ = 'Loadstar'
from pixivpy3 import AppPixivAPI
import os
import sys
import io
import time
import datetime
import json
import re
import hashlib
import random
import zipfile
import shutil
import sqlite3
import argparse
from dateutil import parser
from PIL import Image
from contextlib import contextmanager

sys.stdout = io.TextIOWrapper(
    open(sys.stdout.fileno(), 'wb', 0),
    encoding='utf-8',
    write_through=True,
)
sys.stderr = io.TextIOWrapper(
    open(sys.stderr.fileno(), 'wb', 0),
    encoding='utf-8',
    write_through=True
)

if not os.path.exists("config.py"):
    print("错误: 未找到 config.py 配置文件。\n请先运行 set_config 进行初始化配置。")
    input("\n按回车退出...")
    sys.exit(1)

# 优先处理路径配置
if getattr(sys, 'frozen', False):
    # 打包环境：获取exe所在目录
    base_path = os.path.dirname(sys.executable) 
    # 导入config需要特殊处理
    sys.path.insert(0, base_path)  # 添加exe目录到模块搜索路径
    from config import DOWNLOAD_DIR as original_download_dir
    # 将相对路径转换为绝对路径
    if os.path.isabs(original_download_dir):
        download_dir = original_download_dir
    else:
        download_dir = os.path.join(base_path, original_download_dir)
else:
    # 开发环境：直接从config导入
    from config import DOWNLOAD_DIR as download_dir

# 确保下载目录存在（无论是否打包）
os.makedirs(download_dir, exist_ok=True)

from config import (
    REFRESH_TOKEN,
    USER_ID,
    PROXY,
    DEBUG_API_RESPONSE,
    EXCLUDE_MANGA,
    EXCLUDE_TAGS,
    RANKING_MAX_ITEMS,
    FOLLOW_MAX_ITEMS,
    REQUEST_INTERVAL,
    OUTPUT_FORMAT,
    QUALITY
)

class DBCache:
    def __init__(self, root_dir=download_dir, db_name='pixiv_cache.db'):
        self.db_path = os.path.join(root_dir, db_name)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """获取数据库连接（增加超时和错误处理）"""
        conn = None
        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30,
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            yield conn
            conn.commit()
        except sqlite3.Error as e:
            print(f"[数据库错误] {str(e)}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _init_db(self):
        """初始化全新数据库结构"""
        with self._get_connection() as conn:
            try:
                # 作品缓存表
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS illust_cache (
                        cache_key TEXT PRIMARY KEY CHECK(length(cache_key) > 0),
                        illust_id INTEGER NOT NULL,
                        page_idx INTEGER NOT NULL CHECK(page_idx >= 0),
                        priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 10),
                        file_path TEXT NOT NULL UNIQUE CHECK(length(file_path) > 0),
                        file_size INTEGER NOT NULL CHECK(file_size >= 0),
                        tags_json TEXT CHECK(json_valid(tags_json)),
                        created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                        updated_at DATETIME DEFAULT (datetime('now', 'localtime'))
                    )
                ''')

                # 下载进度表
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS download_progress (
                        user_id TEXT PRIMARY KEY CHECK(length(user_id) > 0),
                        next_qs TEXT CHECK(json_valid(next_qs)),
                        updated_at DATETIME DEFAULT (datetime('now', 'localtime'))
                    )
                ''')

                # 创建索引
                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_cache_key 
                    ON illust_cache (cache_key)
                ''')
                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_priority 
                    ON illust_cache (priority DESC)
                ''')
                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_user 
                    ON download_progress (user_id)
                ''')

            except sqlite3.Error as e:
                import traceback
                print(f"[数据库错误详情]\n{traceback.format_exc()}")
                raise      

    def get_all_progress(self):
        """获取所有下载进度记录"""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute('''
                    SELECT user_id, updated_at 
                    FROM download_progress
                    ORDER BY updated_at DESC
                ''')
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"[数据库错误] 无法获取进度列表: {str(e)}")
            return []

    def check_cache(self, illust_id, page_idx, priority):
        """检查缓存是否存在且有效"""
        if page_idx is None:  # 新增动图判断逻辑
            page_idx = 0
        cache_key = f"illust_{illust_id}_p{page_idx}"
        try:
            with self._get_connection() as conn:
                row = conn.execute('''
                    SELECT file_path, file_size, priority 
                    FROM illust_cache 
                    WHERE cache_key = ?
                ''', (cache_key,)).fetchone()

                if not row:
                    return False

                # 检查优先级
                if priority > row['priority']:
                    return False

                # 检查文件实际状态
                if not os.path.exists(row['file_path']):
                    self.delete_cache(cache_key)
                    return False

                actual_size = os.path.getsize(row['file_path'])
                if actual_size != row['file_size'] or actual_size < 1024*10:
                    self.delete_cache(cache_key)
                    return False

                return True

        except Exception as e:
            print(f"[缓存检查错误] {str(e)}")
            return False

    def update_cache(self, illust_id, page_idx, priority, file_path, tags=None):
        """更新缓存记录"""
        cache_key = f"illust_{illust_id}_p{page_idx}"
        file_size = os.path.getsize(file_path)
        tags_json = json.dumps([tag.lower().strip() for tag in (tags or [])], ensure_ascii=False)

        try:
            with self._get_connection() as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO illust_cache 
                    (cache_key, illust_id, page_idx, priority, file_path, file_size, tags_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    cache_key,
                    illust_id,
                    page_idx,
                    priority,
                    file_path,
                    file_size,
                    tags_json
                ))
            return True
        except sqlite3.Error as e:
            print(f"[缓存更新失败] SQL错误: {str(e)}")
            print(f"参数详情: "
                f"illust_id={illust_id}, page_idx={page_idx}, "
                f"priority={priority}, path={file_path}")
            return False
        except Exception as e:
            print(f"[缓存更新失败] 未知错误: {str(e)}")
            return False

    def _is_tag_filtered(self, cache_key, current_exclude_tags):
        with self._get_connection() as conn:
            cursor = conn.execute('''
                SELECT tags_json
                FROM illust_cache 
                WHERE cache_key = ?''', (cache_key,))
            result = cursor.fetchone()
            
            if not result or not result['tags_json']:  # 修改这里 tags -> tags_json
                return False
                
            try:
                # 简化处理逻辑
                cached_tags = set(json.loads(result['tags_json']))  # 修改这里 tags -> tags_json
                exclude_tags = set(current_exclude_tags)
                
                print(f"[CACHE DEBUG] 缓存标签：{cached_tags}")
                print(f"[CACHE DEBUG] 排除标签：{exclude_tags}")
                
                return len(cached_tags & exclude_tags) > 0
                
            except json.JSONDecodeError:
                return False

    def clear_following_cache(self, user_id=None):
        """清理关注缓存"""
        base_pattern = os.path.join("following", "%")
        params = [f"%{base_pattern}%"]
        where_clause = "file_path LIKE ?"

        if user_id:
            user_pattern = os.path.join("following", f"%_{user_id}")
            params.append(f"%{user_pattern}%")
            where_clause += " AND file_path LIKE ?"

        return self._clear_cache(where_clause, params)

    def clear_bookmarks_cache(self):
        """清理收藏缓存"""
        pattern = os.path.join("bookmarks", "%")
        return self._clear_cache("file_path LIKE ?", [f"%{pattern}%"])

    def clear_ranking_cache(self, category=None, mode_name=None):
        """清理排行榜缓存"""
        base_path = os.path.join("ranking", "%")

        if category:
            base_path = os.path.join("ranking", category, "%")
            if mode_name:
                base_path = os.path.join("ranking", category, f"%{mode_name}%")

        return self._clear_cache("file_path LIKE ?", [f"%{base_path}%"])

    def clear_search_cache(self, search_word=None):
        """清理搜索缓存"""
        base_pattern = os.path.join("search", "%")
        params = [f"%{base_pattern}%"]
        where_clause = "file_path LIKE ?"

        if search_word:
            clean_word = re.sub(r'[\\/*?:"<>|]', '_', search_word.strip())
            word_pattern = os.path.join("search", f"{clean_word}%")
            params.append(f"%{word_pattern}%")
            where_clause += " AND file_path LIKE ?"

        return self._clear_cache(where_clause, params)

    def clear_all_cache(self):
        """清空所有缓存"""
        return self._clear_cache("1=1", [])

    def _clear_cache(self, where_clause, params):
        """通用清理方法"""
        with self._get_connection() as conn:
            try:
                cursor = conn.cursor()
                sql = f"DELETE FROM illust_cache WHERE {where_clause}"
                cursor.execute(sql, params)
                conn.commit()
                return cursor.rowcount
            except sqlite3.Error as e:
                print(f"[缓存清理失败] {str(e)}")
                return 0
    
    def delete_cache(self, illust_id, page_idx=None):
        """通用缓存删除方法（基于作品ID）"""
        cache_key_pattern = f"illust_{illust_id}"
        params = []
        
        # 构建查询条件
        if page_idx is not None:
            where_clause = "cache_key = ?"
            cache_key = f"{cache_key_pattern}_p{page_idx}"
            params.append(cache_key)
        else:
            where_clause = "cache_key LIKE ?"
            params.append(f"{cache_key_pattern}%")

        # 调用现有清理方法
        return self._clear_cache(where_clause, params)

    def get_cache_count(self):
        """获取缓存总数"""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM illust_cache")
            return cursor.fetchone()[0]
        
    def save_progress(self, user_id, params):
        """即时保存进度，增加同步锁，支持包含 next_qs 和 downloaded_ids 的完整 dict"""
        try:
            # 参数验证
            if not isinstance(params, dict):
                raise ValueError("参数必须为字典")
            if 'user_id' not in params:
                params['user_id'] = str(user_id)

            with self._get_connection() as conn:
                conn.execute("PRAGMA synchronous = NORMAL;")
                conn.execute('''
                    INSERT OR REPLACE INTO download_progress 
                    (user_id, next_qs)
                    VALUES (?, ?)
                ''', (str(user_id), json.dumps(params)))
                conn.commit()
            print(f"进度已更新", end="\n", flush=True)
        except Exception as e:
            print(f"进度保存失败: {str(e)}", end="\n", flush=True)

            # 写入本地回退文件
            with open(f"{user_id}_progress.bak", 'w', encoding='utf-8') as f:
                f.write(json.dumps(params, ensure_ascii=False))


    def load_progress(self, user_id):
        """加载完整进度（包含 next_qs、downloaded_ids 等字段）"""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute('''
                    SELECT next_qs FROM download_progress
                    WHERE user_id = ?
                ''', (str(user_id),))
                result = cursor.fetchone()
                if result:
                    return json.loads(result['next_qs'])  # 直接解析完整 dict

            # 数据库无记录时尝试本地备份
            backup_file = f"{user_id}_progress.bak"
            if os.path.exists(backup_file):
                with open(backup_file, encoding='utf-8') as f:
                    return json.loads(f.read())
            return None
        except Exception as e:
            print(f"进度加载失败: {str(e)}")
            return None

    def clear_progress(self, user_id):
        print(f"清除 {user_id} 的进度")
        with self._get_connection() as conn:
            conn.execute("PRAGMA synchronous = OFF;")
            conn.execute('DELETE FROM download_progress WHERE user_id = ?', (user_id,))
            conn.commit()

class PixivDownloader:
    def __init__(self, refresh_token, user_id, root_dir=download_dir, proxies=None, **kwargs):
        self.api = AppPixivAPI(proxies=proxies)
        self.api.auth(refresh_token=refresh_token)
        self.user_id = user_id
        self.root_dir = root_dir
        self.db = DBCache(root_dir=root_dir)

        # 初始化目录
        self.ranking_dir = os.path.join(root_dir, "ranking")
        self.following_dir = os.path.join(root_dir, "following")
        self.bookmarks_dir = os.path.join(root_dir, "bookmarks")
        self.search_dir = os.path.join(root_dir, "search")
        os.makedirs(self.ranking_dir, exist_ok=True)
        os.makedirs(self.following_dir, exist_ok=True)
        os.makedirs(self.bookmarks_dir, exist_ok=True)
        os.makedirs(self.search_dir, exist_ok=True)

        self.ranking_max = kwargs.get('ranking_max', 100)
        self.follow_max = kwargs.get('follow_max', 100)
        self.request_interval = kwargs.get('request_interval', 2)
        self.exclude_tags = {
            tag.strip().lower()  # 仅做标准化处理
            for tag in kwargs.get('exclude_tags', [])
        }
        # 修复的API调试钩子
        if DEBUG_API_RESPONSE:
            self._enable_api_debug()
        # 新增格式转换参数
        self.output_formats = kwargs.get('output_format', 'original')
        self.quality = kwargs.get('quality', 90) 

        # 其他初始化代码保持不变...
        self.exclude_manga = kwargs.get('exclude_manga', True)
        self.chunk_size = 4 * 1024 * 1024
        self.max_buffer_size = 16 * 1024 * 1024  # 16MB内存缓冲
        self.write_buffer = b''
        self.headers = {
            'Referer': 'https://www.pixiv.net/',
            'User-Agent': 'PixivAndroidApp/5.0.234 (Android 11; Pixel 5)',
            'Accept-Encoding': 'gzip, deflate'
        }       
        
        self.clean_temp_files()

    def _enable_api_debug(self):
        """修复版API调试钩子"""
        original_get = self.api.requests.get

        def debug_wrapper(url, **kwargs):
            """新的调试包装函数"""
            print(f"\n[API DEBUG] 请求URL: {url}")
            start = time.time()
            try:
                # 添加默认请求头
                headers = kwargs.get('headers', {})
                headers.update(self.headers)
                kwargs['headers'] = headers
                
                res = original_get(url, **kwargs)
            except Exception as e:
                print(f"请求异常: {str(e)}")
                raise

            elapsed = (time.time() - start)*1000
            print(f"响应状态: {res.status_code} 耗时: {elapsed:.2f}ms")
            print("响应头:")
            for k, v in res.headers.items():
                print(f"  {k}: {v}")

            # 处理大文件内容
            if int(res.headers.get('Content-Length', 0)) > 1024*1024:  # 1MB
                print("响应内容过大已省略")
                return res

            try:
                json_data = res.json()
                print("JSON响应摘要:")
                if 'ugoira_metadata' in json_data:
                    print("  ugoira_metadata结构:")
                    meta = json_data['ugoira_metadata']
                    print(f"    src: {meta.get('src')}")
                    print(f"    mime_type: {meta.get('mime_type')}")
                    print(f"    frames: {len(meta.get('frames', []))}")
                else:
                    print(json.dumps(json_data, indent=2, ensure_ascii=False)[:1000] + "...")
            except json.JSONDecodeError:
                print(f"原始响应内容（前500字符）:\n{res.text[:500]}")

            return res

        self.api.requests.get = debug_wrapper

    def clean_temp_files(self):
        """清理残留临时文件"""
        for root, _, files in os.walk(self.root_dir):
            for f in files:
                if f.endswith('.tmp'):
                    temp_path = os.path.join(root, f)
                    try:
                        os.remove(temp_path)
                        print(f"清理残留文件：{f}")
                    except Exception as e:
                        print(f"清理失败：{f} ({str(e)})")

    def _get_illust_info(self, illust_id):
        """带重试机制的详情获取"""
        retry_count = 3
        for attempt in range(retry_count):
            try:
                res = self.api.illust_detail(illust_id)
                if res.illust and res.illust.id == illust_id:
                    return res.illust
                raise ValueError("Invalid illust response")
            except Exception as e:
                if attempt == retry_count - 1:
                    print(f"获取作品信息失败：{illust_id} - {str(e)}")
                    return None
                time.sleep(2**attempt)

    def _get_illust_pages(self, illust):
        """统一分页索引生成规则（修复动图处理）"""
        if getattr(illust, 'type', '') == 'ugoira':
            # 返回空列表，强制走动图下载流程
            return []
        if illust.page_count == 1:
            return [illust.meta_single_page.get('original_image_url')]
        return [p.image_urls.original for p in illust.meta_pages]

    def _get_illust_tags(self, illust):
        """获取作品的日语原生标签（统一为日语）"""
        tags = []
        try:
            # 确保处理不同类型的标签结构
            tag_list = getattr(illust, 'tags', []) or []
            
            for item in tag_list:
                # 处理不同API版本的结构
                if isinstance(item, dict):
                    # 新版API结构（字典形式）
                    raw_tag = item.get('name', '')  # 原生日语标签
                elif hasattr(item, 'name'):
                    # 旧版API结构（对象属性）
                    raw_tag = item.name
                else:
                    # 未知结构，转换为字符串处理
                    raw_tag = str(item)
                
                # 清洗标签
                if raw_tag:
                    # 直接使用原始标签，移除不必要的转码
                    clean_tag = raw_tag.strip().lower()
                    clean_tag = re.sub(r'[\x00-\x1f]', '', clean_tag)  # 移除控制字符
                    
                    if clean_tag and clean_tag not in tags:  # 去重
                        tags.append(clean_tag)
            
            # 添加作品类型标签（日语）
            if self._is_manga(illust):
                tags.append('漫画')
                
        except Exception as e:
            print(f"标签提取异常：{str(e)}")
            
        return tags


    def _validate_file(self, path, expected_size=None):
        """增强版文件校验"""
        try:
            if not os.path.exists(path):
                print(f"文件不存在: {path}")
                return False
                
            actual_size = os.path.getsize(path)
            if expected_size and actual_size != expected_size:
                print(f"大小不匹配：预期{expected_size} 实际{actual_size}")
                return False
                
            ext = os.path.splitext(path)[1].lower()
            
            if ext == '.zip':
                with zipfile.ZipFile(path) as z:
                    corrupt = z.testzip()
                    if corrupt is not None:
                        print(f"ZIP文件损坏：{corrupt}")
                        return False
                return True
            elif ext in ('.jpg', '.jpeg', '.png'):
                try:
                    with Image.open(path) as img:
                        img.verify()
                        img.load()
                        if img.width < 50 or img.height < 50:
                            print("无效的图片尺寸")
                            return False
                    return True
            
                except Exception as img_e:
                    print(f"图片校验失败：{str(img_e)}")
                    return False
            elif ext == '.gif':
                try:
                    # 使用上下文管理器确保文件正确关闭
                    with Image.open(path) as img:
                        # 检查是否为动画GIF
                        if not getattr(img, 'is_animated', False):
                            print("非动态GIF文件")
                            return False
                        # 快速校验前两帧
                        img.seek(0)
                        img.load()
                        if img.size[0] < 50 or img.size[1] < 50:
                            return False
                        img.seek(1)
                        img.load()
                    return True
                except Exception as e:
                    print(f"GIF校验失败: {str(e)}")
                    return False
            else:
                return actual_size > 1024 * 10
        except Exception as e:
            print(f"文件校验异常：{str(e)}")
            return False

    def _has_excluded_tags(self, illust):
        if not self.exclude_tags:
            print("\n[标签检查] 当前未设置排除标签")
            return False

        tags = self._get_illust_tags(illust)
        illust_tags = set(tags)
        
        print(f"\n[标签检查] 作品标签：{illust_tags}", end="\n", flush=True)
        print(f"[标签检查] 排除标签：{self.exclude_tags}", end="\n", flush=True)
        
        intersection = illust_tags & self.exclude_tags
        if intersection:
            print(f"发现屏蔽标签：{intersection}", end="\n", flush=True)
        return bool(intersection)

    def _is_manga(self, illust):
        """综合漫画检测策略"""
        # 基础类型检测
        if getattr(illust, 'type', '') == 'manga':
            return True
        
        # # 系列作品检测
        # if getattr(illust, 'series', None) is not None:
        #     return True
            
        # 标签检测
        tags = getattr(illust, 'tags', [])
        if '漫画' in tags or 'manga' in tags:
            return True            
        return False

    def _is_confirmed(self,illust,match_num):

        num = int(getattr(illust, 'total_bookmarks', 0))
        return num >= int(match_num)

    def _download_file(self, url, path, priority):
        """优化的文件下载方法（使用初始化参数）"""
        temp_path = f"{path}.{os.getpid()}.tmp"
        expected_size = 0
        attempts = 3
        retry_wait = [3, 8, 15]  # 优化重试间隔
        
        headers = self.headers.copy()
        headers['Referer'] = 'https://www.pixiv.net/'

        for attempt in range(attempts):
            try:
                # ==== 阶段1：获取文件元数据 ====
                try:
                    # 先尝试HEAD方法获取大小
                    with self.api.requests.head(url, headers=headers, timeout=10) as res:
                        res.raise_for_status()
                        expected_size = int(res.headers.get('Content-Length', 0))
                except Exception as head_error:
                    if DEBUG_API_RESPONSE:
                        print(f"HEAD请求失败，尝试GET获取大小: {str(head_error)}")
                    # HEAD失败时使用带流式传输的GET获取
                    with self.api.requests.get(url, headers=headers, stream=True, timeout=10) as res:
                        res.raise_for_status()
                        expected_size = int(res.headers.get('Content-Length', 0))
                        res.close()  # 主动关闭连接

                # ==== 阶段2：准备下载 ====
                print(f"\n开始下载 [{priority}]：{os.path.basename(path)}", end="\n", flush=True)
                if DEBUG_API_RESPONSE:
                    size_info = (f"\n[DEBUG]{expected_size/1024:.1f}KB" if expected_size < 1024*1024*10 
                                else f"{expected_size/1024/1024:.1f}MB")
                    print(f"\n[DEBUG]文件大小: {size_info} | 分块大小: {self.chunk_size//1024}KB", end="\n", flush=True)
                    print(f"\n[DEBUG]预期大小：{expected_size//1024}KB", end="\n", flush=True)

                downloaded = 0
                
                # ==== 阶段3：执行下载 ====
                with self.api.requests.get(url, headers=headers, stream=True, timeout=30) as res:
                    res.raise_for_status()
                    
                    with open(temp_path, 'wb') as f:
                        for chunk in res.iter_content(chunk_size=self.chunk_size):
                            if chunk:
                                f.write(chunk)                   
                                downloaded += len(chunk)
                                
                                  
                                # 自动选择显示单位
                                display_unit = 'MB' if downloaded > 1024*1024 else 'KB'
                                display_size = downloaded/1024/1024 if display_unit == 'MB' else downloaded/1024                                  
                                
                                print(f"\r下载进度: {display_size:.2f}{display_unit}",end="", flush=True)

                        # 写入剩余缓冲
                        if self.write_buffer:
                            f.write(self.write_buffer)
                            self.write_buffer = b''

                # 增强校验（包含大小和基本内容验证）
                if not self._validate_file(temp_path, expected_size):
                    raise ValueError("文件校验失败")
                
                os.replace(temp_path, path)
                return True

            except Exception as e:
                print(f"下载失败（尝试 {attempt+1}/{attempts}）：{str(e)}", end="\n", flush=True)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                if attempt < attempts-1:
                    wait = retry_wait[attempt]
                    print(f"{wait}秒后重试...", end="\n", flush=True)
                    time.sleep(wait)
        
        print(f"无法完成下载：{os.path.basename(path)}")
        return False

    def convert_image(self, original_path):
        """根据配置转换图像格式（支持original保留原格式）"""
        try:
            # 如果配置为original则直接返回
            if str(self.output_formats).lower().strip() == "original":
                print(f"\n保留原始格式: {os.path.basename(original_path)}", end="\n", flush=True)
                return [original_path]

            # ================= 格式配置处理 =================
            FORMAT_MAPPING = {
                'jpg': ('JPEG', '.jpg', 'RGB'),
                'j': ('JPEG', '.jpg', 'RGB'),
                'webp': ('WEBP', '.webp', 'RGB'),
                'w': ('WEBP', '.webp', 'RGB'),
                'png': ('PNG', '.png', 'RGBA'),
                'p': ('PNG', '.png', 'RGBA')
            }

            # 统一配置处理（支持字符串和列表）
            raw_format = self.output_formats
            if isinstance(raw_format, list) and len(raw_format) > 0:
                fmt = str(raw_format[0]).lower().strip()
            else:
                fmt = str(raw_format).lower().strip()

            # 处理格式别名
            fmt = {'g': 'jpg', 'j': 'jpg', 'w': 'webp', 'p': 'png'}.get(fmt, fmt)
            
            # 获取目标格式参数
            if fmt in FORMAT_MAPPING:
                pillow_fmt, file_ext, color_mode = FORMAT_MAPPING[fmt]
                print(f"\n目标格式: {fmt.lower()}")
            else:
                print(f"无效格式配置: {fmt}，使用默认JPG")
                pillow_fmt, file_ext, color_mode = FORMAT_MAPPING['jpg']

            # ================= 核心转换逻辑 =================
            base_name = os.path.splitext(original_path)[0]
            output_path = f"{base_name}{file_ext}"
            
            with Image.open(original_path) as img:
                # 透明度处理
                if img.mode in ('RGBA', 'LA') and color_mode == 'RGB':
                    print("处理透明度通道", end="\n", flush=True)
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[-1])
                    convert_img = background
                else:
                    convert_img = img

                # 色彩模式转换
                if convert_img.mode != color_mode:
                    print(f"转换色彩模式: {convert_img.mode} → {color_mode}", end="\n", flush=True)
                    convert_img = convert_img.convert(color_mode)

                # 保存参数
                save_params = {}
                if pillow_fmt == 'JPEG':
                    quality = min(max(self.quality, 10), 95)
                    save_params = {
                        'quality': quality,
                        'optimize': True,
                        'subsampling': 0  # 强制使用4:4:4避免报错
                    }
                    print(f"JPEG质量参数: Q{quality}")
                elif pillow_fmt == 'WEBP':
                    save_params['quality'] = min(self.quality, 100)
                
                # 安全保存
                temp_path = f"{output_path}.tmp"
                try:
                    convert_img.save(temp_path, format=pillow_fmt, **save_params)
                    os.replace(temp_path, output_path)
                    print(f"转换成功: {os.path.basename(output_path)}", end="\n", flush=True)
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)

                # 清理原文件（仅当扩展名不同时）
                if not original_path.endswith(file_ext):
                    try:
                        os.remove(original_path)
                    except Exception as e:
                        print(f"删除原文件失败: {str(e)}", end="\n", flush=True)

                return [output_path]

        except Exception as e:
            print(f"转换失败: {str(e)}", end="\n", flush=True)
            return []
    
    def download_image(self, illust, page_idx, url, save_path, priority):
        """修改后的下载方法（包含格式转换）"""
        illust_id = None
        try:
            illust_id = illust.id
            tags = self._get_illust_tags(illust)
            
            cache_key = f"illust_{illust_id}_{page_idx}"
            
            # 检查缓存有效性
            if self.db.check_cache(illust_id, page_idx, priority):
                if self.db._is_tag_filtered(cache_key, self.exclude_tags):
                    print(f"⇩ 删除过期缓存（标签变更）", end="\n", flush=True)
                    self.db.delete_cache(illust_id, page_idx)
                    return False
                else:
                    print(f"⇩ 已缓存 [P{priority}]: {os.path.basename(save_path)}", end="\n", flush=True)
                    return False
                
            # 执行下载
            if self._download_file(url, save_path, priority):
                # 转换格式
                converted_files = []
                if self.output_formats:
                    converted_files = self.convert_image(save_path)
                
                # 确定最终缓存路径
                final_path = save_path
                if converted_files:
                    # 如果原始文件被删除，使用第一个转换格式作为缓存路径
                    if not os.path.exists(save_path):
                        final_path = converted_files[0]
                        
                # 更新缓存
                self.db.update_cache(illust_id, page_idx, priority, final_path, tags)
                print(f"下载成功")
                return True
            return False
                  
        except Exception as e:
            print(f"下载失败: {str(e)}")
            if illust_id is not None:  # 安全删除缓存
                self.db.delete_cache(illust_id, page_idx)
            else:
                print("无法获取作品ID，跳过缓存清理", end="\n", flush=True)
            return False

    def download_ugoira(self, illust, save_dir, priority):
        """基于最新CDN路径的动图下载方法（增强错误处理和日志）"""
        try:
            illust_id = illust.id
            cache_key = f"illust_{illust_id}_p0"
            
            # 优先检查缓存（增加文件有效性验证）
            if self.db.check_cache(illust_id, 0, priority):
                cached_path = os.path.join(save_dir, f"{illust_id}.gif")
                if os.path.exists(cached_path):
                    print(f"⇩ 已缓存动图 [P{priority}]: {illust_id}", end="\n", flush=True)
                    return True
                else:
                    self.db.delete_cache(cache_key)

            print(f"\n▶ 开始处理动图作品：{illust_id}", end="\n", flush=True)
            
            # 增强元数据获取（带重试机制）
            metadata = None
            for _ in range(3):
                try:
                    metadata = self.api.ugoira_metadata(illust_id)
                    if metadata and hasattr(metadata, 'ugoira_metadata'):
                        break
                except Exception as e:
                    print(f"获取元数据失败（重试{_+1}/3）: {str(e)}", end="\n", flush=True)
                    time.sleep(2)
            
            if not metadata or not hasattr(metadata, 'ugoira_metadata'):
                raise ValueError("无法获取ugoira元数据，可能API响应结构变化")
            
            frames = metadata.ugoira_metadata.frames
            if not frames:
                raise ValueError("元数据中没有帧信息")

            # 增强CDN路径构造
            create_date = getattr(illust, 'create_date', '')
            try:
                dt = parser.parse(create_date).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
                date_path = dt.strftime("%Y/%m/%d/%H/%M/%S")
            except Exception as e:
                print(f"日期解析失败，使用当前时间: {str(e)}")
                dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
                date_path = dt.strftime("%Y/%m/%d/%H/%M/%S")

            # 多分辨率尝试
            resolutions = ['1920x1080', '1200x1200', '600x600']
            zip_url = None
            for res in resolutions:
                test_url = f"https://i.pximg.net/img-zip-ugoira/img/{date_path}/{illust_id}_ugoira{res}.zip"
                if self.api.requests.head(test_url, headers=self.headers).status_code == 200:
                    zip_url = test_url
                    break

            if not zip_url:
                raise ValueError("找不到有效的动图ZIP资源")

            # 下载ZIP文件（增强校验）
            zip_path = os.path.join(save_dir, f"{illust_id}.zip")
            print(f"下载动图ZIP: {zip_url}")
            if not self._download_with_retry(zip_url, zip_path, self.headers, priority):
                raise ValueError("ZIP文件下载失败")

            # 校验ZIP文件
            if not zipfile.is_zipfile(zip_path):
                raise ValueError("下载的文件不是有效的ZIP格式")
            
            # 处理压缩包（增强异常处理）
            temp_dir = os.path.join(save_dir, f"temp_{illust_id}")
            os.makedirs(temp_dir, exist_ok=True)
            
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    # 验证文件数量与元数据匹配
                    if len(zf.namelist()) != len(frames):
                        raise ValueError("ZIP文件帧数与元数据不一致")
                    
                    # 优先尝试解压到内存
                    extracted_files = []
                    for frame in frames:
                        try:
                            content = zf.read(frame['file'])
                            output_path = os.path.join(temp_dir, frame['file'])
                            with open(output_path, 'wb') as f:
                                f.write(content)
                            extracted_files.append(output_path)
                        except KeyError:
                            raise ValueError(f"ZIP中缺少帧文件: {frame['file']}")

                # 生成GIF（增强参数校验）
                gif_path = os.path.join(save_dir, f"{illust_id}.gif")
                self._create_animated_gif(temp_dir, frames, gif_path)
                
                # 严格验证输出文件
                if not os.path.exists(gif_path) or os.path.getsize(gif_path) < 1024:
                    raise ValueError("生成的GIF文件无效")
                
                # 更新缓存（增加文件校验）
                if self._validate_file(gif_path):
                    self.db.update_cache(illust_id, 0, priority, gif_path, self._get_illust_tags(illust))
                    return True
                return False
                
            except Exception as e:
                print(f"处理过程中出现错误: {str(e)}")
                # 清理不完整文件
                for f in extracted_files:
                    if os.path.exists(f):
                        os.remove(f)
                raise
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
                if os.path.exists(zip_path):
                    os.remove(zip_path)
                    
        except Exception as e:
            print(f"动图处理失败: {str(e)}", end="\n", flush=True)
            import traceback
            traceback.print_exc()  # 打印完整堆栈信息
            return False

    def _download_with_retry(self, url, path, headers, priority, retries=3):
        """带CDN刷新的下载器"""
        temp_path = f"{path}.tmp"
        for attempt in range(retries):
            try:
                # 每次尝试添加不同随机参数
                final_url = f"{url}?rand={random.randint(1000,9999)}" if attempt > 0 else url
                
                with self.api.requests.get(final_url, headers=headers, stream=True, timeout=30) as res:
                    res.raise_for_status()
                    total_size = int(res.headers.get('Content-Length', 0))

                    print(f"\n开始下载 [{priority}]：{os.path.basename(path)}", end="\n", flush=True)
                    print(f"最终地址: {res.url}", end="\n", flush=True)  # 显示实际下载地址
                    print(f"预期大小: {total_size//1024}KB", end="\n", flush=True)

                    downloaded = 0
                    with open(temp_path, 'wb') as f:
                        for chunk in res.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                print(f"\r下载进度：{downloaded/1024/1024:.2f}MB", end="", flush=True)

                    # 严格校验
                    if total_size > 0 and abs(downloaded - total_size) > 1024:
                        raise ValueError(f"大小差异超过1KB: {downloaded} vs {total_size}")
                    
                    os.replace(temp_path, path)
                    print(f"\n载成功!", end="\n", flush=True)
                    return True

            except Exception as e:
                print(f"\n下载失败（尝试 {attempt+1}/{retries}）: {str(e)}", end="\n", flush=True)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                time.sleep([2, 5, 10][attempt])
        
        return False

    def _download_ugoira_file(self, url, path, illust_id, priority):
        """动图专用下载方法"""
        temp_path = f"{path}.ugoira.tmp"
        headers = {
            'Referer': f'https://www.pixiv.net/artworks/{illust_id}',
            'User-Agent': 'PixivAndroidApp/5.0.234 (Android 11; Pixel 5)',
            'Accept-Encoding': 'gzip, deflate, br'
        }

        for attempt in range(3):
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

                with self.api.requests.get(url, headers=headers, stream=True, timeout=30) as res:
                    res.raise_for_status()
                    total_size = int(res.headers.get('Content-Length', 0))

                    print(f"\n开始下载 [{priority}]：{os.path.basename(path)}", end="\n", flush=True)
                    print(f"来源URL: {res.url}", end="", flush=True)  # 显示最终重定向URL
                    print(f"预期大小：{total_size//1024}KB", end="\n", flush=True)

                    downloaded = 0
                    with open(temp_path, 'wb') as f:
                        for chunk in res.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                print(f"\r下载进度：{downloaded/1024/1024:.2f}MB", end="", flush=True)

                    # 严格校验文件
                    if total_size > 0 and downloaded != total_size:
                        raise ValueError(f"大小不匹配：下载{downloaded}字节，预期{total_size}字节")
                    if downloaded < 1024*100:  # 小于100KB视为无效
                        raise ValueError("文件大小异常")

                    os.replace(temp_path, path)
                    print(f"\n下载成功 [{priority}]: {os.path.basename(path)}", end="\n", flush=True)
                    return True

            except Exception as e:
                print(f"\n下载失败（尝试 {attempt+1}/3）: {str(e)}", end="\n", flush=True)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                time.sleep([5, 15, 30][attempt])
        
        return False

    def _create_animated_gif(self, temp_dir, frames, output_path):
        """优化版GIF生成（修复延迟处理）"""
        print(f"生成GIF动画：{output_path}")
        
        # 确保输出路径使用.gif扩展名
        output_path = os.path.splitext(output_path)[0] + '.gif'
        
        images = []
        delays = []
        try:
            # 加载并验证所有帧
            for idx, frame in enumerate(frames, 1):
                frame_file = os.path.join(temp_dir, frame['file'])
                if not os.path.exists(frame_file):
                    raise FileNotFoundError(f"缺少帧文件: {frame['file']}")
                
                with Image.open(frame_file) as img:
                    # 转换为RGB模式并保留透明度处理
                    if img.mode in ('RGBA', 'LA'):
                        alpha = img.split()[-1]
                        bg = Image.new('RGB', img.size, (255, 255, 255))
                        bg.paste(img.convert('RGB'), mask=alpha)
                        images.append(bg)
                    else:
                        images.append(img.convert('RGB'))
                    
                    # 转换延迟时间为百分秒（Pixiv使用毫秒）
                    delay = frame.get('delay', 100)
                    delays.append(delay // 10)  # 转换为百分秒

            # 两阶段保存优化
            temp_path = f"{output_path}.tmp"
            
            # 使用优化参数
            images[0].save(
                temp_path,
                format='GIF',
                save_all=True,
                append_images=images[1:],
                duration=delays,
                loop=0,
                optimize=True,
                disposal=2,
                background=255,
                transparency=255,
                dither=Image.FLOYDSTEINBERG
            )

            # 验证输出文件
            if os.path.getsize(temp_path) < 1024:
                raise ValueError("生成的GIF文件过小")
                
            # 原子操作替换文件
            os.replace(temp_path, output_path)
            print(f"GIF生成成功，大小：{os.path.getsize(output_path)//1024}KB", end="\n", flush=True)
            
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise ValueError(f"GIF生成失败: {str(e)}")
    
    def get_all_following_users(self):
        """修复后的获取关注用户方法"""
        users = []
        next_qs = {'user_id': self.user_id, 'restrict': 'public'}
        
        try:
            while True:
                res = self.api.user_following(**next_qs)
                if not res.user_previews:
                    break
                users.extend(res.user_previews)
                
                next_qs = self.api.parse_qs(res.next_url) or {}
                if not next_qs:
                    break
                time.sleep(1)
        except Exception as e:
            print(f"获取关注列表失败: {str(e)}", end="\n", flush=True)
        
        return users

    def _sanitize_name(self, name, user_id):
        """清洗非法字符并限制长度"""
        clean_username = re.sub(r'[\\/*?:"<>|]', '_', name.strip()).strip('_') 
        if not clean_username:
            clean_username = f"user_{user_id}"
        else:
            # 限制用户名长度（新增）
            clean_username = clean_username[:50]
        return clean_username

    def download_user_illusts(self, target_user_id, username):
        try:
            # 增强用户名清洗逻辑（新增）
            clean_username = self._sanitize_name(username,target_user_id)

            save_dir = os.path.join(self.following_dir, f"{clean_username}_{target_user_id}")
            
            try:  # 新增目录创建验证
                os.makedirs(save_dir, exist_ok=True)
                # 验证目录是否实际存在（新增）
                if not os.path.isdir(save_dir):
                    raise OSError(f"Directory creation failed: {save_dir}")
            except OSError as e:
                print(f"无法创建目录 {save_dir}: {str(e)}", end="\n", flush=True)
                return

            user_id_str = str(target_user_id)
            progress_data = self.db.load_progress(user_id_str) or {}
            current_qs = progress_data.get('next_qs') or {'user_id': target_user_id,"filter": "for_android"}
            downloaded_ids = set(progress_data.get('downloaded_ids', []))
            total = skipped_cache = skipped_tag = skipped_manga = success = 0

            if progress_data:
                print(f"继续上次进度 (已下载 {len(downloaded_ids)} 个作品)")
            
            while True:
                try:                       
                    # 获取作品列表
                    res = self.api.user_illusts(**current_qs)
                    if not res.illusts:
                        print("没有更多作品")
                        self.db.clear_progress(user_id_str)
                        break
                 
                    has_new_content = False 

                    for illust in res.illusts:
                        try:
                            if illust.is_deleted:
                                continue

                            illust_id = illust.id
                            total += 1

                            # 严格缓存检查
                            pages = self._get_illust_pages(illust)
                            has_valid_cache = all(
                                self.db.check_cache(illust_id, idx, 9)
                                for idx in range(len(pages)))
                            
                            if has_valid_cache:
                                if illust_id not in downloaded_ids:
                                    downloaded_ids.add(illust_id)
                                    skipped_cache += 1
                                    print(f"⇩ 发现缓存作品 {illust_id}，更新进度")
                                    self.db.save_progress(user_id_str, {
                                        'next_qs': current_qs,
                                        'downloaded_ids': list(downloaded_ids)
                                    })
                                continue                   
   
                            if self._has_excluded_tags(illust):
                                print(f"排除含屏蔽标签作品：{illust_id}", end="\n", flush=True)
                                skipped_tag += 1
                                continue

                            # 漫画类型处理
                            if self.exclude_manga and illust.type == 'manga':
                                print(f"跳过漫画作品：{illust_id}", end="\n", flush=True)
                                skipped_manga += 1
                                continue
                            
                            download_success = True
                            has_new_content = True 

                            # 动图类型处理
                            if illust.type == 'ugoira':
                                if not self.download_ugoira(illust, save_dir, 9):  # 日志在download_ugoira中打印
                                    download_success = False
                            else:                                              
                                # ===== 普通图片处理 =====
                                for idx, url in enumerate(pages):
                                    if not url:
                                        print(f"关注作品{illust_id}URL无效")                               
                                    fname = os.path.basename(url)
                                    save_path = os.path.join(save_dir, fname)
                                    if not self.download_image(illust, idx, url, save_path, 9):
                                        download_success = False
                                        break                                   
                            
                            # ===== 新增：立即保存进度 =====
                            if download_success:
                                downloaded_ids.add(illust_id)
                                success +=1
                                self.db.save_progress(user_id_str, {
                                    'next_qs': current_qs,
                                    'downloaded_ids': list(downloaded_ids)
                                })
                            
                            time.sleep(self.request_interval)

                        except Exception as e:
                            print(f"作品 {illust_id}处理失败: {str(e)}")

                    # 修改后的进度保存逻辑
                    next_qs = self.api.parse_qs(res.next_url) if res.next_url else None
                    if next_qs:
                        current_qs.update(next_qs)
                        current_qs['user_id'] = target_user_id
                        if has_new_content:
                            print("保存分页进度")
                            self.db.save_progress(user_id_str, {
                                'next_qs': current_qs,
                                'downloaded_ids': list(downloaded_ids)
                            })
                    else:
                        if len(downloaded_ids) > 0:
                                print(f"所有分页已完成，清除进度")
                                self.db.clear_progress(user_id_str)      
                        break

                except KeyboardInterrupt:
                    print("\n用户中断，保存当前进度")
                    self.db.save_progress(user_id_str, {
                        'next_qs': current_qs,
                        'downloaded_ids': list(downloaded_ids)
                    })
                    break

            print(f"用户 {clean_username} 下载统计:")
            print(f"- 总作品数: {total}")
            print(f"- 成功数: {success}")
            print(f"- 跳过已缓存作品数: {skipped_cache}")
            print(f"- 跳过屏蔽作品数: {skipped_tag}")           
            if self.exclude_manga:
                print(f"- 跳过漫画作品数: {skipped_manga}")

        except Exception as e:
            print(f"用户作品下载失败: {str(e)}")
            self.db.save_progress(user_id_str, {
                'next_qs': current_qs,
                'downloaded_ids': list(downloaded_ids)
            })

    def download_following_new(self):
        """从关注用户的新作品专用接口下载"""
        try:           
            jst = datetime.timezone(datetime.timedelta(hours=9))
            now_jst = datetime.datetime.now(jst)
            ranking_date = now_jst.date() - datetime.timedelta(days=1) if now_jst.hour < 12 else now_jst.date()
            date_str = ranking_date.strftime('%Y-%m-%d')

            save_dir = self.following_dir
            os.makedirs(save_dir, exist_ok=True)

            user_id_str = f"following_new_{date_str}"
            progress_data = self.db.load_progress(user_id_str) or {}
            current_qs = progress_data.get('next_qs') or {}
            downloaded_ids = set(progress_data.get('downloaded_ids', []))
            
            print(f"\n▶ 正在下载 关注用户新作品")
            if progress_data:
                print(f"继续上次进度 (已下载 {len(downloaded_ids)} 个作品)")        

            next_qs = {'restrict': 'public'} 
            total = len(downloaded_ids)
            success = 0
            skipped_cache = skipped_tag = skipped_manga = 0 
            
            while total < self.follow_max:
                try:
                    res = self.api.illust_follow(**current_qs)
                    if not res.illusts:
                        break
                        
                    has_new_content = False

                    # 按用户分组作品
                    user_works = {}
                    for illust in res.illusts:
                        user_id = illust.user.id
                        if user_id not in user_works:
                            user_works[user_id] = {
                                'name': illust.user.name,
                                'works': []
                            }
                        user_works[user_id]['works'].append(illust)
                    
                    # 为每个用户创建目录并下载
                    for uid, data in user_works.items():
                        user_dir = os.path.join(save_dir, f"{self._sanitize_name(data['name'],uid)}_{uid}")
                        os.makedirs(user_dir, exist_ok=True)
                        
                        for illust in data['works']:
                            try:
                                if illust.is_deleted:
                                    continue

                                illust_id = illust.id
                                total += 1

                                pages = self._get_illust_pages(illust)
                                has_valid_cache = all(
                                    self.db.check_cache(illust_id, idx, 9)
                                    for idx in range(len(pages)))
                                
                                if has_valid_cache:
                                    if illust_id not in downloaded_ids:
                                        downloaded_ids.add(illust_id)
                                        skipped_cache += 1
                                        print(f"⇩ 发现缓存作品 {illust_id}，更新进度")
                                        self.db.save_progress(user_id_str, {
                                            'next_qs': current_qs,
                                            'downloaded_ids': list(downloaded_ids)
                                        })                                
                                        continue

                                if self._has_excluded_tags(illust):
                                    print(f"排除含屏蔽标签作品：{illust_id}")
                                    skipped_tag += 1
                                    continue

                                if self.exclude_manga and self._is_manga(illust):
                                    print(f"跳过漫画作品：{illust_id}", end="\n", flush=True)
                                    skipped_manga += 1
                                    continue
                    

                                download_success = True
                                has_new_content = True

                                if illust.type == 'ugoira':
                                    if not self.download_ugoira(illust, user_dir, 9):
                                        download_success = False                        
                                else:                              
                                    for idx, url in enumerate(pages):
                                        if not url:
                                            print(f"作品{illust_id}URL无效")   
                                            continue
                                        fname = os.path.basename(url)
                                        save_path = os.path.join(user_dir, fname)
                                        if not self.download_image(illust, idx, url, save_path, 9):
                                            download_success = False
                
                                if download_success:
                                    downloaded_ids.add(illust_id)
                                    success += 1
                                    self.db.save_progress(user_id_str, {
                                        'next_qs': current_qs,
                                        'downloaded_ids': list(downloaded_ids)
                                    })

                                if total >= self.follow_max:
                                    print(f"达到最大数量限制 {self.follow_max}")
                                    break  

                                time.sleep(self.request_interval)

                            except Exception as e:
                                print(f"处理作品失败 {illust.id}: {str(e)}", end="\n", flush=True)

                        if total >= self.follow_max:
                            break  

                    # 处理分页
                    next_qs = self.api.parse_qs(res.next_url) if res.next_url else None
                    if total >= self.follow_max:
                        print(f"已下载{total}个作品，达到上限，清除进度")
                        self.db.clear_progress(user_id_str)
                        break
                    elif next_qs:
                        current_qs.update(next_qs)
                        if has_new_content:
                            print("保存分页进度")
                            self.db.save_progress(user_id_str, {
                                'next_qs': current_qs,
                                'downloaded_ids': list(downloaded_ids)
                            })
                    else:
                        if len(downloaded_ids)> 0:
                            print("所有分页已完成，清除进度")
                            self.db.clear_progress(user_id_str)
                        break

                except KeyboardInterrupt:
                    print("\n用户中断，保存进度...")
                    self.db.save_progress(user_id_str, {
                        'next_qs': current_qs,
                        'downloaded_ids': list(downloaded_ids),
                    })
                    break
            # 最终处理
            print(f"关注用户新作品下载下载统计:")
            print(f"- 总作品: {total}")
            print(f"- 成功数: {success}")
            print(f"- 跳过已缓存作品数: {skipped_cache}")
            print(f"- 跳过屏蔽作品数: {skipped_tag}")
            if self.exclude_manga:
                print(f"- 跳过漫画作品数: {skipped_manga}")

        except Exception as e:
            print(f"下载失败: {str(e)}")
            self.db.save_progress(user_id_str, {
                'next_qs': current_qs,
                'downloaded_ids': list(downloaded_ids)
            })                      


    def download_bookmarks(self):
        """下载收藏（带进度管理）"""
        try:
            user_id_str = f"bookmarks_{self.user_id}"
            progress_data = self.db.load_progress(user_id_str) or {}
            current_qs = progress_data.get('next_qs') or {'user_id': self.user_id,"filter": "for_android"}
            downloaded_ids = set(progress_data.get('downloaded_ids', []))
            total = success = 0

            print("\n▶ 正在下载收藏作品...", end="\n", flush=True)
            if progress_data:
                print(f"继续上次进度 (已下载 {len(downloaded_ids)} 个作品)")

            total = skipped_cache = skipped_tag = skipped_manga = success = 0
 
            while True:
                try:
                    res = self.api.user_bookmarks_illust(**current_qs)
                    if not res.illusts:
                        print("没有更多收藏作品")
                        self.db.clear_progress(user_id_str)
                        break
                
                    has_new_content = False

                    for illust in res.illusts:
                        try:
                            if illust.is_deleted:
                                continue

                            illust_id = illust.id
                            total += 1

                            # 严格缓存检查
                            pages = self._get_illust_pages(illust)
                            has_valid_cache = all(
                                self.db.check_cache(illust_id, idx, 10)
                                for idx in range(len(pages)))
                            
                            if has_valid_cache:
                                if illust_id not in downloaded_ids:
                                    downloaded_ids.add(illust_id)
                                    skipped_cache += 1
                                    print(f"⇩ 发现缓存作品 {illust_id}，更新进度")
                                    self.db.save_progress(user_id_str, {
                                        'next_qs': current_qs,
                                        'downloaded_ids': list(downloaded_ids)
                                    })                                
                                    continue

                            download_success = True
                            has_new_content = True

                            if illust.type == 'ugoira':
                                if not self.download_ugoira(illust, self.bookmarks_dir, 10):
                                    download_success = False                        
                            else:                                                      
                                for idx, url in enumerate(pages):
                                    if not url:
                                        print(f"收藏作品{illust.id}URL无效")
                                        continue
                                    fname = os.path.basename(url)
                                    save_path = os.path.join(self.bookmarks_dir, fname)
                                    if not self.download_image(illust, idx, url, save_path, 10):
                                        download_success = False
                            
                            if download_success:
                                downloaded_ids.add(illust_id)
                                success +=1
                                # 每完成一个作品立即保存进度
                                self.db.save_progress(user_id_str, {
                                    'next_qs': current_qs,
                                    'downloaded_ids': list(downloaded_ids)
                                })

                            time.sleep(self.request_interval)

                        except Exception as e:
                            print(f"处理收藏作品失败 {illust.id}: {str(e)}", end="\n", flush=True)


                    next_qs = self.api.parse_qs(res.next_url) if res.next_url else None
                    if next_qs:
                        current_qs.update(next_qs)
                        current_qs['user_id'] = self.user_id
                        if has_new_content:
                            print("保存分页进度")
                            self.db.save_progress(user_id_str, {
                                'next_qs': current_qs,
                                'downloaded_ids': list(downloaded_ids)
                            })
                    else:
                        if len(downloaded_ids) > 0:
                            print(f"所有分页已完成，清除进度")
                            self.db.clear_progress(user_id_str)
                        break

                except KeyboardInterrupt:
                    print("\n用户中断，保存进度...")
                    self.db.save_progress(user_id_str, {
                        'next_qs': current_qs,
                        'downloaded_ids': list(downloaded_ids),
                    })
                    break               

            print(f"收藏下载下载统计:")
            print(f"- 总作品: {total}")
            print(f"- 成功数: {success}")
            print(f"- 跳过已缓存作品数: {skipped_cache}")

        except Exception as e:
            print(f"收藏下载失败: {str(e)}")
            self.db.save_progress(user_id_str, {
                'next_qs': current_qs,
                'downloaded_ids': list(downloaded_ids)
            })

    def download_ranking(self, mode, category, mode_name, priority):
        """智能排行榜下载（支持分页和漫画过滤）"""
        try:
            # 准备存储目录（使用category参数）
            jst = datetime.timezone(datetime.timedelta(hours=9))
            now_jst = datetime.datetime.now(jst)
            ranking_date = now_jst.date() - datetime.timedelta(days=1) if now_jst.hour < 12 else now_jst.date()
            date_str = ranking_date.strftime('%Y-%m-%d')
            
            save_dir = os.path.join(self.ranking_dir, category, f"{date_str}_{mode_name}")
            os.makedirs(save_dir, exist_ok=True)

            user_id_str = f"ranking_{category}_{mode}_{ranking_date}"
            progress_data = self.db.load_progress(user_id_str) or {}
            current_qs = progress_data.get('next_qs') or {'mode': mode,"filter": "for_android"}
            downloaded_ids = set(progress_data.get('downloaded_ids', []))

 
            print(f"\n▶ 正在下载 {category} {mode_name}（模式：{mode}）")
            if progress_data:
                print(f"继续上次进度 (已下载 {len(downloaded_ids)} 个作品)")
            next_qs = {'mode': mode}

            total = len(downloaded_ids)
            success = 0
            skipped_cache = skipped_tag = skipped_manga = 0 

            while total < self.ranking_max:
                try:
                    res = self.api.illust_ranking(**current_qs) 
                    if not res or not hasattr(res, 'illusts'):
                        print("API响应异常，等待重试...")
                        time.sleep(5)
                        continue

                    if not res.illusts:               
                        print("没有更多排行榜作品")
                        self.db.clear_progress(user_id_str)
                        break

                    has_new_content = False

                    for illust in res.illusts:
                        try:
                            if illust.is_deleted:
                                continue

                            illust_id = illust.id
                            total += 1

                            # 严格缓存检查
                            pages = self._get_illust_pages(illust)
                            has_valid_cache = all(
                                self.db.check_cache(illust_id, idx, priority)
                                for idx in range(len(pages)))
                            
                            if has_valid_cache:
                                if illust_id not in downloaded_ids:
                                    downloaded_ids.add(illust_id)
                                    skipped_cache += 1
                                    print(f"⇩ 发现缓存作品 {illust_id}，更新进度")
                                    self.db.save_progress(user_id_str, {
                                        'next_qs': current_qs,
                                        'downloaded_ids': list(downloaded_ids)
                                    })                                
                                    continue

                            if self._has_excluded_tags(illust):
                                print(f"排除含屏蔽标签作品：{illust_id}")
                                skipped_tag += 1
                                continue

                            if self.exclude_manga and self._is_manga(illust):
                                print(f"跳过漫画作品：{illust_id}", end="\n", flush=True)
                                skipped_manga += 1
                                continue

                            download_success = True
                            has_new_content = True


                            if illust.type == 'ugoira':
                                if not self.download_ugoira(illust, save_dir, priority):
                                    download_success = False                        
                            else:                              
                                for idx, url in enumerate(pages):
                                    if not url:
                                        print(f"排行榜作品{illust_id}URL无效")   
                                        continue
                                    fname = os.path.basename(url)
                                    save_path = os.path.join(save_dir, fname)
                                    if not self.download_image(illust, idx, url, save_path, priority):
                                        download_success = False
            
                            if download_success:
                                downloaded_ids.add(illust_id)
                                success += 1
                                self.db.save_progress(user_id_str, {
                                    'next_qs': current_qs,
                                    'downloaded_ids': list(downloaded_ids)
                                })

                            if total >= self.ranking_max:
                                print(f"达到最大数量限制 {self.ranking_max}")
                                break  

                            time.sleep(self.request_interval)

                        except Exception as e:
                            print(f"处理作品失败 {illust.id}: {str(e)}", end="\n", flush=True)

                    # 处理分页
                    next_qs = self.api.parse_qs(res.next_url) if res.next_url else None
                    if total >= self.ranking_max:
                        print(f"已下载{total}个作品，达到上限，清除进度")
                        self.db.clear_progress(user_id_str)
                        break
                    elif next_qs:
                        current_qs.update(next_qs)
                        current_qs['mode'] = mode
                        if has_new_content:
                            print("保存分页进度")
                            self.db.save_progress(user_id_str, {
                                'next_qs': current_qs,
                                'downloaded_ids': list(downloaded_ids)
                            })
                    else:
                        if len(downloaded_ids)> 0:
                            print("所有分页已完成，清除进度")
                            self.db.clear_progress(user_id_str)
                        break                         

                except KeyboardInterrupt:
                    print("\n用户中断，保存进度...")
                    self.db.save_progress(user_id_str, {
                        'next_qs': current_qs,
                        'downloaded_ids': list(downloaded_ids),
                    })
                    break
   
            # 最终处理
            print(f"{category}_{mode}排行榜下载下载统计:")
            print(f"- 总作品: {total}")
            print(f"- 成功数: {success}")
            print(f"- 跳过已缓存作品数: {skipped_cache}")
            print(f"- 跳过屏蔽作品数: {skipped_tag}")
            if self.exclude_manga:
                print(f"- 跳过漫画作品数: {skipped_manga}")

        except Exception as e:
            print(f"排行榜下载失败: {str(e)}")
            self.db.save_progress(user_id_str, {
                'next_qs': current_qs,
                'downloaded_ids': list(downloaded_ids)
            })
    
    def download_search(self, search_word, search_target='partial_match_for_tags', 
                    sort='date_desc', duration=None, exclude_ai=True,
                    exclude_18=False, num_choice=True):
        try:
            # 提取收藏数标签
            match = re.search(r"(\d+)users入り$", search_word)
            use_num_tag = num_choice and match is not None
            match_num = match.group(1) if match else None

            # 清理搜索词（保留收藏数标签）
            if use_num_tag:
                clean_word = re.sub(r'[\\/*?:"<>|]', '_', search_word.strip()).lower()
            else:
                temp_word = re.sub(r"\d+users入り$", "", search_word).strip()
                clean_word = re.sub(r'[\\/*?:"<>|]', '_', temp_word.strip()).lower()

            # 创建保存目录
            save_dir = os.path.join(self.search_dir, clean_word)
            os.makedirs(save_dir, exist_ok=True)

            # 初始化基础参数
            base_qs = {
                'word': clean_word,
                'search_target': search_target,
                'sort': sort,
                'filter': 'for_android'
            }
            if duration:
                base_qs['duration'] = duration

            # 进度管理
            user_id_str = f"search_{clean_word}"
            progress_data = self.db.load_progress(user_id_str) or {}
            downloaded_ids = set(progress_data.get('downloaded_ids', []))
            total = success = 0
            skipped_cache = skipped_tag = skipped_manga = skipped_ai = 0

            print(f"\n▶ 正在搜索下载: {search_word}")
            if progress_data:
                print(f"继续上次进度 (已下载 {len(downloaded_ids)} 个作品)")

            # ================== 分流处理逻辑 ==================
            if use_num_tag:
                # 高效模式（禁用时间窗口）
                current_qs = base_qs.copy()
                if 'offset' in progress_data.get('next_qs', {}):
                    current_qs['offset'] = progress_data['next_qs']['offset']
                if 'duration' in progress_data.get('next_qs', {}):
                    current_qs['duration'] = progress_data['next_qs']['duration']

                while True:
                    try:
                        res = self.api.search_illust(**current_qs)
                        if not res.illusts:
                            print("没有更多结果")
                            self.db.clear_progress(user_id_str)
                            break

                        has_new_content = False
                        for illust in res.illusts:
                            try:
                                # [原有的作品处理逻辑，保持不动]
                                # 包括：标签检查、下载等...
                                
                                # 示例处理流程（需保持原有逻辑）：
                                if illust.is_deleted:
                                    continue

                                illust_id = getattr(illust, 'id', '未知ID')

                                # 收藏数验证
                                if self._is_confirmed(illust, match_num) != True:
                                    print(f"排除不符合收藏数作品：{illust_id}", end="\n", flush=True)
                                    continue
                                
                                # 过滤检查
                                if exclude_18 and 'r-18' in self._get_illust_tags(illust):
                                    print(f"排除R-18作品：{illust_id}", end="\n", flush=True)
                                    continue
                                    
                                # 下载处理...
                                total += 1

                                # 严格缓存检查
                                pages = self._get_illust_pages(illust)
                                has_valid_cache = all(
                                    self.db.check_cache(illust_id, idx, 9)
                                    for idx in range(len(pages)))
                                
                                if has_valid_cache:
                                    if illust_id not in downloaded_ids:
                                        downloaded_ids.add(illust_id)
                                        skipped_cache += 1
                                        print(f"⇩ 发现缓存作品 {illust_id}，更新进度")
                                        self.db.save_progress(user_id_str, {
                                            'next_qs': current_qs,
                                            'downloaded_ids': list(downloaded_ids),
                                            'current_window': {
                                            'start': start_date.isoformat(),
                                            'end': end_date.isoformat()
                                            }
                                        })
                                    continue  

                                if self._has_excluded_tags(illust):
                                    print(f"排除含屏蔽标签作品：{illust_id}", end="\n", flush=True)
                                    skipped_tag += 1
                                    continue
                                
                                # AI类型处理
                                if exclude_ai and illust.illust_ai_type == 2:
                                    print(f"跳过AI作品：{illust_id}", end="\n", flush=True)
                                    skipped_ai += 1
                                    continue

                                # 漫画类型处理
                                if self.exclude_manga and illust.type == 'manga':
                                    print(f"跳过漫画作品：{illust_id}", end="\n", flush=True)
                                    skipped_manga += 1
                                    continue                 
                                
                                download_success = True
                                has_new_content = True 

                                # 动图类型处理
                                if illust.type == 'ugoira':
                                    if not self.download_ugoira(illust, save_dir, 9):  # 日志在download_ugoira中打印
                                        download_success = False
                                else:                                              
                                    # ===== 普通图片处理 =====
                                    for idx, url in enumerate(pages):
                                        if not url:
                                            print(f"搜索作品{illust_id}URL无效")                               
                                        fname = os.path.basename(url)
                                        save_path = os.path.join(save_dir, fname)
                                        if not self.download_image(illust, idx, url, save_path, 9):
                                            download_success = False
                                            break                                   
                                
                                # ===== 新增：立即保存进度 =====
                                if download_success:
                                    downloaded_ids.add(illust_id)
                                    success +=1
                                    self.db.save_progress(user_id_str, {
                                        'next_qs': current_qs,
                                        'downloaded_ids': list(downloaded_ids),
                                        'current_window': {
                                        'start': start_date.isoformat(),
                                        'end': end_date.isoformat()
                                        }
                                    })
                                
                                time.sleep(self.request_interval)
                                
                            except Exception as e:
                                print(f"作品处理失败: {str(e)}")

                        # 更新分页参数
                        next_qs = self.api.parse_qs(res.next_url)
                        if next_qs:
                            current_qs.update(next_qs)
                            print("保存分页进度")
                            if 'duration' in base_qs:
                                current_qs['duration'] = base_qs['duration']
                            self.db.save_progress(user_id_str, {
                                'next_qs': current_qs,
                                'downloaded_ids': list(downloaded_ids),
                                'current_window': None  # 明确标记非时间窗口模式
                            })
                        else:
                            if len(downloaded_ids)> 0:
                                print("所有分页已完成，清除进度")
                                self.db.clear_progress(user_id_str)
                            break

                        # 请求间隔
                        time.sleep(self.request_interval)

                    except KeyboardInterrupt:
                        print("\n用户中断，保存分页进度")
                        self.db.save_progress(user_id_str, {
                            'next_qs': current_qs,
                            'downloaded_ids': list(downloaded_ids),
                            'current_window': None
                        })
                        break

            else:
                # 时间窗口模式（原有逻辑，但已优化）
                jst = datetime.timezone(datetime.timedelta(hours=9))
                window_size = datetime.timedelta(days=30)
                
                # 初始化时间窗口
                current_window = progress_data.get('current_window')
                if current_window:
                    start_date = parser.parse(current_window['start']).astimezone(jst)
                    end_date = parser.parse(current_window['end']).astimezone(jst)
                else:
                    end_date = datetime.datetime.now(jst).replace(hour=0, minute=0, second=0)
                    start_date = end_date - window_size

                while True:
                    print(f"\n当前时间窗口: {start_date.date()} 至 {end_date.date()}")

                    # 构造时间参数
                    current_qs = base_qs.copy()
                    current_qs.update({
                        'start_date': start_date.strftime('%Y-%m-%d'),
                        'end_date': end_date.strftime('%Y-%m-%d')
                    })
                    if duration:
                        current_qs.pop('duration', None) 
                    # [原有时间窗口内的分页处理逻辑，保持不动]
                    # 包括分页循环、作品处理等...


                    while True:
                        try:
                            res = self.api.search_illust(**current_qs)
                            if not res.illusts:
                                print("当前时间窗口无更多结果")
                                break

                            has_new_content = False
                            for illust in res.illusts:
                                try:
                                    if illust.is_deleted:
                                        continue

                                    illust_id = getattr(illust, 'id', '未知ID')

                                    if self._is_confirmed(illust,match_num) != True:
                                        print(f"排除不符合收藏数作品：{illust_id}", end="\n", flush=True)
                                        continue

                                    if exclude_18 and 'r-18' in self._get_illust_tags(illust):
                                        print(f"排除R-18作品：{illust_id}", end="\n", flush=True)
                                        continue

                                    total += 1

                                    # 严格缓存检查
                                    pages = self._get_illust_pages(illust)
                                    has_valid_cache = all(
                                        self.db.check_cache(illust_id, idx, 9)
                                        for idx in range(len(pages)))
                                    
                                    if has_valid_cache:
                                        if illust_id not in downloaded_ids:
                                            downloaded_ids.add(illust_id)
                                            skipped_cache += 1
                                            print(f"⇩ 发现缓存作品 {illust_id}，更新进度")
                                            self.db.save_progress(user_id_str, {
                                                'next_qs': current_qs,
                                                'downloaded_ids': list(downloaded_ids),
                                                'current_window': {
                                                'start': start_date.isoformat(),
                                                'end': end_date.isoformat()
                                                }
                                            })
                                        continue    

                                    if self._has_excluded_tags(illust):
                                        print(f"排除含屏蔽标签作品：{illust_id}", end="\n", flush=True)
                                        skipped_tag += 1
                                        continue
                                    
                                    # AI类型处理
                                    if exclude_ai and illust.illust_ai_type == 2:
                                        print(f"跳过AI作品：{illust_id}", end="\n", flush=True)
                                        skipped_ai += 1
                                        continue

                                    # 漫画类型处理
                                    if self.exclude_manga and illust.type == 'manga':
                                        print(f"跳过漫画作品：{illust_id}", end="\n", flush=True)
                                        skipped_manga += 1
                                        continue               
                                    
                                    download_success = True
                                    has_new_content = True 

                                    # 动图类型处理
                                    if illust.type == 'ugoira':
                                        if not self.download_ugoira(illust, save_dir, 9):  # 日志在download_ugoira中打印
                                            download_success = False
                                    else:                                              
                                        # ===== 普通图片处理 =====
                                        for idx, url in enumerate(pages):
                                            if not url:
                                                print(f"搜索作品{illust_id}URL无效")                               
                                            fname = os.path.basename(url)
                                            save_path = os.path.join(save_dir, fname)
                                            if not self.download_image(illust, idx, url, save_path, 9):
                                                download_success = False
                                                break                                   
                                    
                                    # ===== 新增：立即保存进度 =====
                                    if download_success:
                                        downloaded_ids.add(illust_id)
                                        success +=1
                                        self.db.save_progress(user_id_str, {
                                            'next_qs': current_qs,
                                            'downloaded_ids': list(downloaded_ids),
                                            'current_window': {
                                            'start': start_date.isoformat(),
                                            'end': end_date.isoformat()
                                            }
                                        })
                                    
                                    time.sleep(self.request_interval)

                                except Exception as e:
                                    print(f"作品 {illust_id}处理失败: {str(e)}")

                            # 修改后的进度保存逻辑
                            if 'offset' in current_qs:
                                print(f"当前偏移{current_qs['offset']}")
                            next_qs = self.api.parse_qs(res.next_url) if res.next_url else None
                            if next_qs:
                                current_qs.update(next_qs)
                                if has_new_content:
                                    print("保存分页进度")
                                    self.db.save_progress(user_id_str, {
                                        'next_qs': current_qs,
                                        'downloaded_ids': list(downloaded_ids),
                                        'current_window': {
                                        'start': start_date.isoformat(),
                                        'end': end_date.isoformat()
                                        }
                                    })
                            else:
                                print(f"当前窗口分页完成")
                                break

                        except KeyboardInterrupt:
                            print("\n用户中断，保存当前进度")
                            self.db.save_progress(user_id_str, {
                                'next_qs': current_qs,
                                'downloaded_ids': list(downloaded_ids),
                                'current_window': {
                                'start': start_date.isoformat(),
                                'end': end_date.isoformat()
                                }
                            })
                            break

                        except Exception as e:
                            print(f"搜索失败: {str(e)}")

                    # 滑动时间窗口
                    new_end_date = start_date - datetime.timedelta(days=1)
                    new_start_date = new_end_date - window_size + datetime.timedelta(days=1)  # 修正窗口计算

                    # 边界检查
                    if new_start_date < datetime.datetime(2007, 9, 10, tzinfo=jst):
                        print("已搜索到最早时间范围")
                        self.db.clear_progress(user_id_str)
                        break

                    start_date = new_start_date
                    end_date = new_end_date

                    # 保存窗口进度
                    self.db.save_progress(user_id_str, {
                        'next_qs': base_qs,  # 保存基础参数
                        'downloaded_ids': list(downloaded_ids),
                        'current_window': {
                            'start': start_date.isoformat(),
                            'end': end_date.isoformat()
                        }
                    })
                    time.sleep(1)

            # ================== 最终处理 ==================
            if not os.listdir(save_dir):
                print('目录下无文件，删除目录')
                os.rmdir(save_dir)
                self.db.clear_progress(user_id_str)

            print(f"搜索 {search_word} 下载统计:")
            print(f"- 总作品数: {total}")
            print(f"- 成功数: {success}")
            print(f"- 跳过已缓存作品数: {skipped_cache}")
            print(f"- 跳过屏蔽标签作品数: {skipped_tag}")
            if self.exclude_manga:
                print(f"- 跳过漫画作品数: {skipped_manga}")
            if exclude_ai:
                print(f"- 跳过AI作品数: {skipped_ai}")

        except Exception as e:
            print(f"搜索下载失败: {str(e)}")
            self.db.save_progress(user_id_str, {
                'next_qs': current_qs if 'current_qs' in locals() else base_qs,
                'downloaded_ids': list(downloaded_ids),
                'current_window': current_window if not use_num_tag else None
            })
    
        
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def display_main_menu():
    clear_screen()
    print("="*50)
    print("Pixiv智能下载管理器")
    print("="*50)
    print("1. 下载关注用户作品")
    print("2. 下载我的收藏") 
    print("3. 下载排行榜")
    print("4. 搜索下载")
    print("5. 缓存管理")
    print("6. 进度管理")
    print("0. 退出")

def handle_following(downloader):
    """修复后的关注处理逻辑"""
    while True:
        clear_screen()
        print("下载关注用户作品")
        print("1. 下载全部关注")
        print("2. 选择用户下载")
        print("3. 新作品下载")
        print("0. 返回主菜单")
        choice = input("请选择操作：")
        
        if choice == '1':
            try:
                users = downloader.get_all_following_users()
                if not users:
                    input("未关注任何用户！")
                    continue
                
                for u in users:
                    print(f"\n▶ 正在下载 {u.user.name} 的作品...", end="\n", flush=True)
                    downloader.download_user_illusts(u.user.id, u.user.name)
                input("\n操作完成，按回车返回...")
            except Exception as e:
                print(f"发生错误: {str(e)}")
                input("按回车返回...")
            break
            
        elif choice == '2':
            try:
                users = downloader.get_all_following_users()
                if not users:
                    input("未关注任何用户！")
                    break
                
                print("\n关注列表：")
                for i, u in enumerate(users, 1):
                    print(f"{i}. {u.user.name} (ID: {u.user.id})")
                
                sel = input("\n输入要下载的序号（多个用逗号分隔）：")
                indexes = [int(x)-1 for x in sel.split(',') if x.strip().isdigit()]
                selected = [users[i] for i in indexes if 0 <= i < len(users)]
                
                if not selected:
                    input("无效的选择！")
                    continue
                
                for u in selected:
                    print(f"\n▶ 正在下载 {u.user.name} 的作品...", end="\n", flush=True)
                    downloader.download_user_illusts(u.user.id, u.user.name)
                input("\n下载完成，按回车返回...")
                
            except Exception as e:
                print(f"发生错误: {str(e)}")
                input("按回车返回...")
            break

        elif choice == '3':
            try:
                downloader.download_following_new()
                input("\n操作完成，按回车返回...")
            except Exception as e:
                print(f"发生错误: {str(e)}")
                input("按回车返回...")
            
        elif choice == '0':
            break

def handle_ranking(downloader):
    ranking_config = {
        '1': {
            'name': '一般向',
            'modes': [
                ('month', '月榜', 8),
                ('week', '周榜', 7),
                ('day', '日榜', 6),
                ('week_rookie', '新人榜', 5),
                ('week_original', '原创榜', 5),
                ('day_ai', 'AI生成', 5),
                ('day_male', '男性向', 5),
                ('day_female', '女性向', 5)
            ]
        },
        '2': {
            'name': 'R-18',
            'modes': [
                ('week_r18', '周榜', 4),
                ('day_r18', '日榜', 3),
                ('day_r18_ai', 'AI生成', 2),
                ('day_male_r18', '男性向', 2),
                ('day_female_r18', '女性向', 2)
            ]
        }
    }

    while True:
        clear_screen()
        print("\n选择排行榜分类：")
        print("1. 一般向")
        print("2. R-18")
        print("0. 返回上级")
        cat = input("请选择：")
        
        if cat == '0': break
        category = ranking_config.get(cat)
        if not category:
            input("无效选择！")
            continue
        
        while True:
            clear_screen()
            print(f"\n{category['name']}榜单类型：")
            modes = category['modes']
            for i, (_, name, _) in enumerate(modes, 1):
                print(f"{i}. {name}")
            print("0. 返回上级")
            
            choice = input("请选择：")
            if choice == '0':
                break
            
            try:
                idx = int(choice)-1
                mode, name, prio = modes[idx]
                print(f"\n正在下载 {name}...")
                downloader.download_ranking(mode, category['name'], name, prio)
                input("\n完成，按回车继续...")
            except:
                input("无效选择！")

def handle_search(downloader):
    clear_screen()
    print("搜索下载")
    print("人气标签选择")
    pop_amount = input("请输入收藏数(默认无此标签)：").strip()
    num_choice = input("是否将收藏数作为标签？(y/n 默认y):").strip().lower() != 'n'
    search_word = input("请输入搜索关键词：").strip()
    if not pop_amount:
        pop_tag =''
    else:
        pop_tag = f'{pop_amount}users入り'
    combined_search = f"{search_word} {pop_tag}"
    if not combined_search:
        return
    
    print("\n搜索类型：")
    print("1. 标签部分一致")
    print("2. 标签完全一致")
    print("3. 标题说明文")
    mode_choice = input("请选择搜索类型（默认1）：") or '1'
    mode_map = {'1': 'partial_match_for_tags', '2': 'exact_match_for_tags', 
                   '3': 'title_and_caption'}
    search_target = mode_map.get(mode_choice)

    print("\n排序方式：")
    print("1. 按最新排序")
    print("2. 按热门排序")
    sort_choice = input("请选择排序方式（默认1）：") or '1'
    sort = 'date_desc' if sort_choice == '1' else 'popular_desc'

    print("\n时间范围：")
    print("1. 全部时间")
    print("2. 最近一天")
    print("3. 最近一周")
    print("4. 最近一月")
    duration_choice = input("请选择时间范围（默认1）：") or '1'
    duration_map = {'1': None, '2': 'within_last_day', 
                   '3': 'within_last_week', '4': 'within_last_month'}
    duration = duration_map.get(duration_choice)

    exclude_ai = input("是否排除AI作品？(y/n 默认y) ").strip().lower() != 'n'

    exclude_18 = input("是否排除R-18作品？(y/n 默认n) ").strip().lower() == 'y'

    downloader.download_search(
        search_word=combined_search,
        search_target = search_target,
        sort=sort,
        duration=duration,
        exclude_ai=exclude_ai,  # 传递新参数
        exclude_18 = exclude_18,
        num_choice = num_choice
    )
    input("\n按回车返回主菜单...")     

def handle_cache(downloader):
    # 排行榜类型配置
    R18_RANKING_TYPES = {
        '1': ('day_r18', 'R-18日榜'),
        '2': ('week_r18', 'R-18周榜'),
        '3': ('day_r18_ai', 'R-18 AI榜'),
        '4': ('day_male_r18', 'R-18男性向'),
        '5': ('day_female_r18', 'R-18女性向')
    }

    GENERAL_RANKING_TYPES = {
        '1': ('day', '日榜'),
        '2': ('week', '周榜'),
        '3': ('month', '月榜'),
        '4': ('week_rookie', '新人榜'),
        '5': ('week_original', '原创榜'),
        '6': ('week_ai', 'AI榜'),
        '7': ('day_male', '男性向'),
        '8': ('day_female', '女性向')
    }

    def clean_following():
        """处理关注缓存清理"""
        while True:
            clear_screen()
            print("关注用户缓存清理")
            print("1. 清理所有关注缓存")
            print("2. 清理指定用户缓存")
            print("0. 返回上级")
            choice = input("请选择操作：").strip()

            if choice == '1':
                if input("确认清理所有关注用户的缓存？(y/n) ").lower() == 'y':
                    removed = downloader.db.clear_following_cache()
                    print(f"已清理 {removed} 条关注缓存")
                    input("按回车继续...")
                break
            elif choice == '2':
                user_id = input("请输入要清理的用户ID：").strip()
                if user_id:
                    if input(f"确认清理用户 {user_id} 的缓存？(y/n) ").lower() == 'y':
                        removed = downloader.db.clear_following_cache(user_id=user_id)
                        print(f"已清理 {removed} 条用户 {user_id} 的缓存")
                        input("按回车继续...")
                break
            elif choice == '0':
                return
            else:
                print("无效输入！")
                input("按回车继续...")

    def clean_bookmarks():
        """处理收藏缓存清理"""
        if input("确认清理所有收藏缓存？(y/n) ").lower() == 'y':
            removed = downloader.db.clear_bookmarks_cache()
            print(f"已清理 {removed} 条收藏缓存")
            input("按回车继续...")

    def show_ranking_clean_menu(category, type_map):
        """显示指定分类的排行榜清理菜单"""
        while True:
            clear_screen()
            print(f"{category}榜单清理")
            print("a. 清理本类全部缓存")
            for key in sorted(type_map.keys()):
                mode, name = type_map[key]
                print(f"{key}. 清理{name}")
            print("0. 返回上级")
            
            choice = input("请选择操作：").strip().lower()
            
            if choice == '0':
                return
                
            if choice == 'a':
                if input(f"确认清理所有{category}缓存？(y/n) ").lower() == 'y':
                    removed = downloader.db.clear_ranking_cache(category=category)
                    print(f"已清理 {removed} 条{category}缓存")
                    input("按回车继续...")
                continue
                
            if choice in type_map:
                mode, name = type_map[choice]
                if input(f"确认清理【{name}】？(y/n) ").lower() == 'y':
                    # 提取模式名称（如"日榜"）
                    mode_name = name.split()[-1]
                    removed = downloader.db.clear_ranking_cache(
                        category=category,
                        mode_name=mode_name
                    )
                    print(f"已清理 {removed} 条【{name}】缓存")
                    input("按回车继续...")
            else:
                print("无效选择！")
                input("按回车继续...")

    def clean_search():
        """处理搜索缓存清理"""
        while True:
            clear_screen()
            print("搜索缓存清理")
            print("1. 清理所有搜索缓存")
            print("2. 清理指定关键词缓存")
            print("0. 返回上级")
            choice = input("请选择操作：").strip()

            if choice == '1':
                if input("确认清理所有搜索缓存？(y/n) ").lower() == 'y':
                    removed = downloader.db.clear_search_cache()
                    print(f"已清理 {removed} 条搜索缓存")
                    input("按回车继续...")
                break
            elif choice == '2':
                keyword = input("请输入要清理的关键词：").strip()
                if keyword:
                    if input(f"确认清理【{keyword}】的缓存？(y/n) ").lower() == 'y':
                        removed = downloader.db.clear_search_cache(keyword)
                        print(f"已清理 {removed} 条相关缓存")
                        input("按回车继续...")
                break
            elif choice == '0':
                return
            else:
                print("无效输入！")
                input("按回车继续...")

    def clean_ranking():
        """排行榜缓存主菜单"""
        while True:
            clear_screen()
            print("排行榜缓存清理")
            print("1. 一般向作品")
            print("2. R-18作品")
            print("0. 返回上级")
            choice = input("请选择分类：").strip()

            if choice == '1':
                show_ranking_clean_menu('一般向', GENERAL_RANKING_TYPES)
            elif choice == '2':
                show_ranking_clean_menu('R-18', R18_RANKING_TYPES)
            elif choice == '0':
                return
            else:
                print("无效输入！")
                input("按回车继续...")

    while True:
        try:
            clear_screen()
            print("缓存管理系统")
            print("1. 清理关注缓存")
            print("2. 清理收藏缓存")
            print("3. 清理排行榜缓存")
            print("4. 清理搜索缓存")
            print("4. 清理所有缓存")
            print("5. 查看缓存统计")
            print("0. 返回主菜单")
            main_choice = input("请选择操作：").strip()

            if main_choice == '1':
                clean_following()
            elif main_choice == '2':
                clean_bookmarks()
            elif main_choice == '3':
                clean_ranking()
            elif main_choice == '4':
                clean_search()
            elif main_choice == '5':
                if input("确认清理所有缓存？（该操作不可逆）(y/n) ").lower() == 'y':
                    removed = downloader.db.clear_all_cache()
                    print(f"已清除 {removed} 条缓存记录")
                    input("按回车继续...")
            elif main_choice == '6':
                count = downloader.db.get_cache_count()
                print(f"\n当前缓存总量：{count} 条记录")
                input("按回车返回...")
            elif main_choice == '0':
                return
            else:
                print("无效输入！")
                input("按回车继续...")
        except Exception as e:
            print(f"操作失败：{str(e)}")
            import traceback
            traceback.print_exc()
            input("按回车继续...")

def handle_progress(downloader):
    """下载进度管理"""
    while True:
        clear_screen()
        print("下载进度管理")
        print("1. 查看所有进度")
        print("2. 删除指定进度")
        print("0. 返回主菜单")
        choice = input("请选择操作：")

        if choice == '1':
            clear_screen()
            print("当前所有下载进度：")
            progress_list = downloader.db.get_all_progress()
            
            if not progress_list:
                print("暂无进行中的下载任务")
                input("\n按回车返回...")
                continue
                
            for idx, progress in enumerate(progress_list, 1):
                user_id = progress['user_id']
                timestamp = progress['updated_at'].split('.')[0]  # 去除毫秒
                print(f"{idx}. [用户ID: {user_id}] 最后更新时间: {timestamp}")
                
            input("\n按回车返回...")

        elif choice == '2':
            clear_screen()
            print("删除下载进度")
            progress_list = downloader.db.get_all_progress()
            
            if not progress_list:
                print("没有可删除的进度")
                input("\n按回车返回...")
                continue
                
            # 显示可删除列表
            print("可删除的进度：")
            for idx, progress in enumerate(progress_list, 1):
                user_id = progress['user_id']
                timestamp = progress['updated_at'].split('.')[0]
                print(f"{idx}. [用户ID: {user_id}] 最后更新时间: {timestamp}")
                
            # 获取选择
            try:
                sel = input("\n输入要删除的序号（多个用逗号分隔，0返回）：")
                if sel.strip() == '0':
                    continue
                    
                indexes = [int(x)-1 for x in sel.split(',') if x.strip().isdigit()]
                selected = [progress_list[i] for i in indexes if 0 <= i < len(progress_list)]
                
                if not selected:
                    print("无效的选择！")
                    input("\n按回车返回...")
                    continue
                    
                # 确认删除
                confirm = input(f"确认删除 {len(selected)} 条进度？(y/n) ").lower()
                if confirm == 'y':
                    for progress in selected:
                        downloader.db.clear_progress(progress['user_id'])
                        print(f"已删除 {progress['user_id']} 的进度")
                    input("\n删除完成，按回车返回...")
                    
            except Exception as e:
                print(f"操作失败：{str(e)}")
                input("\n按回车返回...")
                
        elif choice == '0':
            break
        else:
            print("无效输入！")
            input("\n按回车返回...")

def execute_ranking_download(downloader, args):
    """更新后的排行榜执行函数（带正确优先级）"""
    priority_map = {
        # 一般向
        'month': 8,
        'week': 7,
        'day': 6,
        'week_rookie': 5,
        'week_original': 5,
        'week_ai': 5,
        'day_male': 5,
        'day_female': 5,
        # R-18
        'week_r18': 4,
        'day_r18': 3,
        'day_r18_ai': 2,
        'day_male_r18': 2,
        'day_female_r18': 2
    }

    # 验证模式有效性
    valid_modes = priority_map.keys()
    if args.mode not in valid_modes:
        print(f"错误：无效的排行榜模式 {args.mode}")
        sys.exit(1)

    # 获取配置信息
    mode_name = {
        'month': '月榜',
        'week': '周榜',
        'day': '日榜',
        'week_rookie': '新人榜',
        'week_original': '原创榜',
        'week_ai': 'AI生成',
        'day_male': '男性向',
        'day_female': '女性向',
        'week_r18': '周榜',
        'day_r18': '日榜',
        'day_r18_ai': 'AI生成',
        'day_male_r18': '男性向',
        'day_female_r18': '女性向'
    }.get(args.mode, args.mode)


    priority = priority_map.get(args.mode, 5)

    print(f"\n▶ 正在下载排行榜：{args.category} - {mode_name}")
    if DEBUG_API_RESPONSE:
        print(f"[DEBUG]参数配置：")
        print(f"├─ 最大数量: {downloader.ranking_max}")
        print(f"├─ 请求间隔: {downloader.request_interval}秒")
        print(f"└─ 下载优先级: {priority}")

    try:
        downloader.download_ranking(
            mode=args.mode,
            category=args.category,
            mode_name=mode_name,
            priority=priority
        )
        print("排行榜下载完成！")
    except Exception as e:
        print(f"下载失败：{str(e)}")
        sys.exit(1)


# 新增函数：处理命令行接口
def handle_command_line():
    """增强的命令行处理"""
    parser = argparse.ArgumentParser(
        description="Pixiv批量下载器命令行模式",
        add_help=False
    )
    
    subparsers = parser.add_subparsers(dest='command', help='可用命令')

    # 排行榜下载命令增强
    ranking_parser = subparsers.add_parser('ranking', help='下载排行榜')
    ranking_parser.add_argument('--mode', required=True, 
                              choices=['week','week_ai','week_original','week_rookie','day','day_male','day_female',
                                         'month', 'week_r18', 'day_r18','day_r18_ai','day_male_r18','day_female_r18'],
                              help='排行榜模式')
    ranking_parser.add_argument('--category', required=True,
                              choices=['一般向', 'R-18'],
                              help='分类名称')
    
    follow_parser = subparsers.add_parser('follow', help='下载关注新作品')

    args = parser.parse_args()

    downloader = PixivDownloader(
        refresh_token=REFRESH_TOKEN,
        user_id=USER_ID,
        root_dir=download_dir,
        proxies=PROXY,
        exclude_manga=EXCLUDE_MANGA,
        exclude_tags=EXCLUDE_TAGS,
        ranking_max=RANKING_MAX_ITEMS,
        follow_max=FOLLOW_MAX_ITEMS,
        request_interval=REQUEST_INTERVAL,
        output_format=OUTPUT_FORMAT,
        quality=QUALITY
    )

    if args.command == 'ranking':
        execute_ranking_download(downloader, args)
    if args.command == 'follow':
        downloader.download_following_new()

def main():

    # 先处理命令行参数
    if len(sys.argv) > 1:
        handle_command_line()
        return
      
    downloader = PixivDownloader(
        refresh_token=REFRESH_TOKEN,
        user_id=USER_ID,
        root_dir=download_dir,
        proxies=PROXY,
        exclude_manga=EXCLUDE_MANGA,
        exclude_tags=EXCLUDE_TAGS,
        ranking_max=RANKING_MAX_ITEMS,
        follow_max=FOLLOW_MAX_ITEMS,
        request_interval=REQUEST_INTERVAL,
        output_format=OUTPUT_FORMAT,
        quality=QUALITY
    )
    
    while True:
        display_main_menu()
        choice = input("\n请输入选项：")
        
        if choice == '1':
            handle_following(downloader)
        elif choice == '2':
            downloader.download_bookmarks()
            input("\n操作完成，按回车返回...")
        elif choice == '3':
            handle_ranking(downloader)
        elif choice == '4':  # 搜索下载
            handle_search(downloader)
        elif choice == '5':  # 原缓存管理选项顺延
            handle_cache(downloader)
        elif choice == '6':  # 新增进度管理
            handle_progress(downloader)
        elif choice == '0':
            print("感谢使用，再见！")
            break
        else:
            input("无效输入，请重新选择！")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"发生错误: {str(e)}")
        import traceback
        traceback.print_exc()
        input("按回车键退出...")  # 新增全局异常处