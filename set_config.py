#!/usr/bin/env python3
# coding=utf-8
' config module '
__author__ = 'Loadstar'
import os
import sys
import shutil
import time
from typing import Dict, List, Any

def get_script_dir() -> str:
    """获取当前运行目录，打包后返回 EXE 所在目录，开发中返回脚本目录"""
    if getattr(sys, 'frozen', False):  # 运行在 PyInstaller 打包后的 EXE 中
        return os.getcwd()
    else:
        return os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG_TEMPLATE = '''# ==== Pixiv 下载器配置 ====
__author__ = 'neverfind'

# 账号配置
REFRESH_TOKEN = "{refresh_token}"
USER_ID = "{user_id}"

# 路径配置
DOWNLOAD_DIR = r"{download_dir}"

# 网络配置
PROXY = {proxy}

# 内容过滤
EXCLUDE_MANGA = {exclude_manga}
EXCLUDE_TAGS = {exclude_tags}

# 输出配置
OUTPUT_FORMAT = "{output_format}"  # 可选：original/jpg/webp
QUALITY = {quality}               # 图片质量1-100（仅对jpg/webp有效）

# 高级配置（需手动修改）
RANKING_MAX_ITEMS = 100      # 榜单最大下载数量
FOLLOW_MAX_ITEMS = 100      # 关注最大下载数量
REQUEST_INTERVAL = 2         # 请求间隔(秒)

# API响应调试
DEBUG_API_RESPONSE = False 
'''

def validate_proxy(proxy_str: str) -> Dict[str, str]:
    """验证并格式化代理配置"""
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return {"http": "", "https": ""}
    
    if not proxy_str.startswith("http"):
        proxy_str = f"http://{proxy_str}"
    
    return {"http": proxy_str, "https": proxy_str}

def get_required_input(prompt: str, is_password: bool = False) -> str:
    """获取必填项输入"""
    print(f"\n※ {prompt}")
    print("   - Windows：右键粘贴 | Mac/Linux: Cmd/Ctrl+Shift+V")
    
    return input(">>> ").strip()

def get_optional_input(prompt: str, default: str = "") -> str:
    """获取可选输入项"""
    hint = f"（默认：{default}）" if default else ""
    print(f"\n※ {prompt}{hint}")
    value = input(">>> ").strip()
    return value if value else default

def select_download_dir() -> str:
    """选择下载目录"""
    print("\n※ 3. 请选择下载目录：")
    print("   1) 使用脚本所在目录（推荐）")
    print("   2) 自定义目录")
    
    while True:
        choice = input(">>> 请选择 (1/2，默认1): ").strip()
        if not choice:
            choice = "1"
        
        if choice == "1":
            return get_script_dir()
        elif choice == "2":
            custom_dir = get_optional_input(
                "请输入自定义目录路径",
                default=r"D:\PIXIV"
            )
            # 替换路径中的环境变量
            expanded_dir = os.path.expanduser(os.path.expandvars(custom_dir))
            # 标准化路径格式
            return os.path.normpath(expanded_dir)
        else:
            print("无效选项，请重新输入")

def init_config():
    """首次配置向导"""
    print("\n" + "="*40)
    print("Pixiv 下载器首次配置向导")
    print("="*40)
    
    config = {
        'refresh_token': get_required_input("1. 请输入 refresh_token", is_password=True),
        'user_id': get_required_input("2. 请输入用户ID"),
        'download_dir': select_download_dir(),
        'proxy': validate_proxy(
            get_optional_input(
                "4. 请输入代理地址（格式：IP:端口）",
                default="127.0.0.1:7890"
            )
        ),
        'exclude_manga': get_optional_input(
            "5. 是否排除漫画作品？(y/n)",
            default="y"
        ).lower() == 'y',
        'exclude_tags': [
            tag.strip().lower()
            for tag in get_optional_input(
                "6. 请输入要屏蔽的标签（英文逗号分隔）",
                default=""
            ).split(",")
            if tag.strip()
        ],
        'output_format': get_optional_input(
            "7. 选择输出格式（original/jpg/webp）",
            default="original"
        ).lower(),
        'quality': max(min(int(
            get_optional_input(
                "8. 设置图片质量（1-100）",
                default="85"
            ) or 85
        ), 100), 1)
    }

    # 验证输出格式
    if config['output_format'] not in ('original', 'jpg', 'webp'):
        print("⚠️ 无效的输出格式，已重置为original")
        config['output_format'] = 'original'

    # 生成配置文件
    config_path = os.path.join(get_script_dir(), "config.py")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(DEFAULT_CONFIG_TEMPLATE.format(**config))
    
    print(f"\n✅ 配置已保存到 {config_path}")
    print("="*40 + "\n")

def edit_config_item(current_value: Any, prompt: str, value_type: type = str):
    """通用配置项编辑"""
    print(f"\n当前值：{current_value}")
    new_value = input(f"{prompt}：").strip()
    
    if not new_value:
        return current_value
    
    try:
        return value_type(new_value)
    except ValueError:
        print(f"⚠️ 无效输入，保持原值")
        return current_value

def edit_download_dir(current_dir: str) -> str:
    """编辑下载目录"""
    print("\n当前下载目录：", current_dir)
    print("请选择：")
    print("1) 保持当前目录")
    print("2) 改为脚本所在目录")
    print("3) 自定义目录")
    
    while True:
        choice = input(">>> 请选择 (1/2/3): ").strip()
        if choice == "1":
            return current_dir
        elif choice == "2":
            return get_script_dir()
        elif choice == "3":
            custom_dir = get_optional_input(
                "请输入新目录路径",
                default=r"D:\PIXIV"
            )
            expanded_dir = os.path.expanduser(os.path.expandvars(custom_dir))
            return os.path.normpath(expanded_dir)
        else:
            print("无效选项，请重新输入")

def edit_config():
    """配置编辑界面"""
    try:
        from config import (
            REFRESH_TOKEN, USER_ID, DOWNLOAD_DIR,
            PROXY, EXCLUDE_MANGA, EXCLUDE_TAGS,
            OUTPUT_FORMAT, QUALITY
        )
    except ImportError:
        print("⚠️ 配置文件损坏，需要重新初始化")
        os.rename("config.py", f"config_bak_{int(time.time())}.py")
        init_config()
        return

    current_config = {
        'refresh_token': REFRESH_TOKEN,
        'user_id': USER_ID,
        'download_dir': DOWNLOAD_DIR,
        'proxy': PROXY,
        'exclude_manga': EXCLUDE_MANGA,
        'exclude_tags': EXCLUDE_TAGS,
        'output_format': OUTPUT_FORMAT,
        'quality': QUALITY
    }

    while True:
        print("\n当前配置：")
        print(f"1. Refresh Token: {'*'*12}（已设置）" if current_config['refresh_token'] else "1. Refresh Token: 未设置")
        print(f"2. 用户ID: {current_config['user_id']}")
        print(f"3. 下载目录: {current_config['download_dir']}")
        print(f"4. 代理设置: {current_config['proxy']['http']}")
        print(f"5. 排除漫画: {'是' if current_config['exclude_manga'] else '否'}")
        print(f"6. 屏蔽标签: {current_config['exclude_tags'] if current_config['exclude_tags'] else '无'}")
        print(f"7. 输出格式: {current_config['output_format'].upper()}")
        print(f"8. 图片质量: {current_config['quality']}")
        print("0. 返回主菜单")
        
        choice = input("\n请选择要修改的项: ").strip()
        
        if choice == '0':
            break
            
        try:
            if choice == '1':
                new_val = get_required_input("新的 Refresh Token", is_password=True)
                current_config['refresh_token'] = new_val
            elif choice == '2':
                new_val = get_required_input("新的用户ID")
                current_config['user_id'] = new_val
            elif choice == '3':
                new_val = edit_download_dir(current_config['download_dir'])
                current_config['download_dir'] = new_val
            elif choice == '4':
                current_proxy = current_config['proxy']['http'].replace("http://", "")
                new_proxy = validate_proxy(
                    edit_config_item(current_proxy, "新的代理地址（格式：IP:端口）")
                )
                current_config['proxy'] = new_proxy
            elif choice == '5':
                new_val = edit_config_item(
                    '是' if current_config['exclude_manga'] else '否',
                    "排除漫画作品？(y/n)"
                ).lower() == 'y'
                current_config['exclude_manga'] = new_val
            elif choice == '6':
                current_tags = current_config['exclude_tags']
                print(f"当前屏蔽标签：{current_tags if current_tags else '无'}")
                new_input = edit_config_item(
                    ', '.join(current_tags),
                    "新的屏蔽标签（英文逗号分隔，使用 -前缀 删除标签，如：new_tag,-old_tag）"
                )
                
                # 处理带删除标记的标签
                remove_tags = []
                add_tags = []
                for tag in new_input.split(','):
                    tag = tag.strip().lower()
                    if not tag:
                        continue
                    if tag.startswith('-'):
                        remove_tag = tag[1:].strip().lower()
                        if remove_tag:
                            remove_tags.append(remove_tag)
                    else:
                        add_tags.append(tag)
                
                # 更新标签列表
                updated_tags = [t for t in current_tags if t not in remove_tags]
                updated_tags.extend(add_tags)
                # 去重并保留原始顺序
                seen = set()
                current_config['exclude_tags'] = [
                    t for t in updated_tags 
                    if not (t in seen or seen.add(t))
                ]
            elif choice == '7':
                new_val = edit_config_item(
                    current_config['output_format'],
                    "新的输出格式（original/jpg/webp）"
                ).lower()
                if new_val in ('original', 'jpg', 'webp'):
                    current_config['output_format'] = new_val
                else:
                    print("⚠️ 无效格式，保持原设置")
            elif choice == '8':
                new_val = max(min(int(edit_config_item(
                    current_config['quality'],
                    "新的图片质量（1-100）"
                )), 100), 1)
                current_config['quality'] = new_val
            else:
                print("无效选项！")
                continue
                
            # 保存修改
            backup_name = f"config_backup_{int(time.time())}.py"
            shutil.copy("config.py", backup_name)
            
            with open("config.py", "w", encoding="utf-8") as f:
                f.write(DEFAULT_CONFIG_TEMPLATE.format(**current_config))
            print(f"✅ 配置已更新（备份：{backup_name}）")
            
        except Exception as e:
            print(f"配置更新失败: {str(e)}")

def check_config():
    """配置检查入口"""
    if not os.path.exists("config.py"):
        init_config()
    else:
        try:
            from config import REFRESH_TOKEN, USER_ID
            if not REFRESH_TOKEN or not USER_ID:
                raise ValueError("重要配置缺失")
        except:
            print("⚠️ 检测到无效配置，需要重新初始化")
            os.rename("config.py", f"config_invalid_{int(time.time())}.py")
            init_config()
        
        edit_config()

if __name__ == "__main__":
    if os.path.basename(__file__) == "config.py":
        print("错误：请通过 setup_config.py 管理配置")
        sys.exit(1)
        
    check_config()