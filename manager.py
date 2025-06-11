#!/usr/bin/env python3
# coding=utf-8
' manager module '
__author__ = 'Loadstar'
import os
import sys
import json
import subprocess
from typing import List, Dict
import importlib.util

# === 动态加载 config.py =========================================
def load_config():
    """动态加载 config.py 并返回模块对象"""
    base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))  # 适配打包后路径
    config_path = os.path.join(base_dir, "config.py")
    if not os.path.exists(config_path):
        print("错误: 未找到 config.py 配置文件。\n请先运行 set_config 进行初始化配置。")
        input("\n按回车退出...")
        sys.exit(1)
    
    spec = importlib.util.spec_from_file_location("config", config_path)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return config

config = load_config()
DOWNLOAD_DIR = config.DOWNLOAD_DIR

# === 配置区 ====================================================
EXE_NAME = "download"
EXE_PATH = ""
PYTHON_PATH = sys.executable  # 当前运行脚本的Python解释器路径

def resolve_exe_path():
    """根据可用文件自动决定使用EXE或PY文件"""
    base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    exe_file = os.path.join(base_dir, f"{EXE_NAME}.exe")
    py_file = os.path.join(base_dir, f"{EXE_NAME}.py")

    print(f"正在检查可执行文件路径：\nEXE路径：{exe_file}\nPY路径：{py_file}")  # 调试信息

    if os.path.isfile(exe_file):
        print("检测到EXE文件")
        return exe_file, False
    elif os.path.isfile(py_file):
        print("检测到PY文件")
        return py_file, True
    else:
        print(f"错误: 未找到 {EXE_NAME}.exe 或 {EXE_NAME}.py")
        sys.exit(1)

EXE_PATH, IS_PYTHON_SCRIPT = resolve_exe_path()
TASK_FILE = os.path.join(DOWNLOAD_DIR, "ranking_tasks.json")  # 任务配置文件
ENCODINGS = ['utf-8', 'gbk', 'cp936']

# === 编码环境初始化 ================================================
sys.stdin.reconfigure(encoding='utf-8', errors='replace')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# === 工具函数 =======================================================
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def safe_decode(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')

# === 任务管理类 =====================================================
class RankingManager:
    def __init__(self):
        self.tasks: List[Dict] = []
        self.load_tasks()

    def load_tasks(self):
        try:
            if os.path.exists(TASK_FILE):
                with open(TASK_FILE, 'rb') as f:
                    raw = f.read()
                    self.tasks = json.loads(safe_decode(raw))
        except Exception as e:
            print(f"配置加载失败: {str(e)}")
            self.tasks = []

    def save_tasks(self):
        try:
            task_dir = os.path.dirname(TASK_FILE)
            os.makedirs(task_dir, exist_ok=True) 

            with open(TASK_FILE, 'wb') as f:
                raw = json.dumps(self.tasks, ensure_ascii=False, indent=2)
                f.write(raw.encode('utf-8'))
            return True
        except Exception as e:
            print(f"配置保存失败: {str(e)}")
            return False

    def add_task(self, mode: str, category: str, name: str):
        if any(t.get('mode') == mode for t in self.tasks):
            print(f"任务 {name} 已存在")
            return False

        base_cmd = []
        if IS_PYTHON_SCRIPT:
            if not os.path.isfile(PYTHON_PATH):
                print(f"错误: Python解释器不存在于 {PYTHON_PATH}")
                return False
            base_cmd = [PYTHON_PATH, EXE_PATH]
        else:
            base_cmd = [EXE_PATH]

        new_task = {
            "mode": mode,
            "category": category,
            "name": name,
            "command": base_cmd + [
                "ranking",
                "--mode", mode,
                "--category", category
            ]
        }
        
        self.tasks.append(new_task)
        return self.save_tasks()
    
    def add_follow_task(self):
        if any(t.get('name') == 'follow' for t in self.tasks):
            print(f"任务已存在")
            return False

        base_cmd = []
        if IS_PYTHON_SCRIPT:
            if not os.path.isfile(PYTHON_PATH):
                print(f"错误: Python解释器不存在于 {PYTHON_PATH}")
                return False
            base_cmd = [PYTHON_PATH, EXE_PATH]
        else:
            base_cmd = [EXE_PATH]

        new_task = {
            "name": 'follow',
            "command": base_cmd + [
                "follow"
            ]
        }
        self.tasks.append(new_task)
        print(f"成功添加任务")
        return self.save_tasks()
        
    def remove_task(self, index: int):
        if 0 <= index < len(self.tasks):
            removed = self.tasks.pop(index)
            print(f"已删除任务: {removed['name']}")
            return self.save_tasks()
        return False

    def execute_task(self, task: Dict):
        print(f"\n{'━'*30}")
        print(f"执行任务: {task['name']}")
        print(f"命令: {' '.join(task['command'])}")
        print(f"{'━'*30}")

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            
            process = subprocess.Popen(
                task["command"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace'
            )

            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    print(line.strip())

            if process.returncode != 0:
                print(f"任务执行失败，退出码: {process.returncode}")
                return False
            return True

        except Exception as e:
            print(f"执行异常: {str(e)}")
            return False

# === 命令行接口 =====================================================
def main_menu(manager: RankingManager):
    while True:
        clear_screen()
        print("═"*50)
        print("Pixiv任务管理器")
        print("═"*50)
        print("1. 添加任务")
        print("2. 添加关注用户新作任务")
        print("3. 删除任务")
        print("4. 列出任务")
        print("5. 执行所有任务")
        print("6. 修改download路径 (当前: {})".format(EXE_PATH))
        print("0. 退出")

        choice = input("\n请选择操作: ").strip()

        if choice == '1':
            add_task_menu(manager)
        elif choice == '2':
            manager.add_follow_task()
        elif choice == '3':
            remove_task_menu(manager)
        elif choice == '4':
            list_tasks(manager)
        elif choice == '5':
            execute_all_tasks(manager)
        elif choice == '6':
            change_exe_path()
        elif choice == '0':
            break
        input("\n按回车继续...")

def add_task_menu(manager: RankingManager):
    clear_screen()
    print("\n选择分类:")
    print("1. 一般向")
    print("2. R-18")
    print("0. 返回")
    
    cat_choice = input("请选择分类: ").strip()
    if cat_choice not in ['1', '2']:
        return

    ranking_config = {
        '1': {
            'name': '一般向',
            'modes': [
                ('month', '月榜'),
                ('week', '周榜'),
                ('day', '日榜'),
                ('week_rookie', '新人榜'),
                ('week_original', '原创榜'),
                ('week_ai', 'AI生成'),
                ('day_male', '男性向'),
                ('day_female', '女性向')
            ]
        },
        '2': {
            'name': 'R-18',
            'modes': [
                ('week_r18', '周榜'),
                ('day_r18', '日榜'),
                ('day_r18_ai', 'AI生成'),
                ('day_male_r18', '男性向'),
                ('day_female_r18', '女性向')
            ]
        }
    }

    category = ranking_config[cat_choice]['name']
    modes = ranking_config[cat_choice]['modes']

    clear_screen()
    print(f"\n{category} 榜单类型:")
    for idx, (mode, name) in enumerate(modes, 1):
        print(f"{idx}. {name}")

    mode_choice = input("请选择榜单类型: ").strip()
    if not mode_choice.isdigit():
        return

    idx = int(mode_choice) - 1
    if 0 <= idx < len(modes):
        mode, name = modes[idx]
        if manager.add_task(mode, category, name):
            print(f"成功添加任务: {name}")
        else:
            print(f"添加任务失败: {name}")

def remove_task_menu(manager: RankingManager):
    clear_screen()
    if not manager.tasks:
        print("当前没有任务")
        return

    print("\n当前任务列表:")
    for idx, task in enumerate(manager.tasks, 1):
        if "ranking" in task["command"]:
            print(f"{idx}. [{task['category']}] {task['name']}")
        elif "follow" in task["command"]:
            print(f"{idx}. {task['name']}")

    choice = input("\n输入要删除的任务序号 (0取消): ").strip()
    if choice.isdigit():
        idx = int(choice) - 1
        manager.remove_task(idx)

def list_tasks(manager: RankingManager):
    clear_screen()
    if not manager.tasks:
        print("当前没有配置任务")
        return

    print("\n已配置任务:")
    for idx, task in enumerate(manager.tasks, 1):
        if "ranking" in task["command"]:
            print(f"{idx}. [{task['category']}] {task['name']}")
        elif "follow" in task["command"]:
            print(f"{idx}. {task['name']}")
        print(f"   命令: {' '.join(task['command'])}")

def execute_all_tasks(manager: RankingManager):
    clear_screen()
    if not manager.tasks:
        print("没有可执行的任务")
        return

    print("即将执行以下任务:")
    for task in manager.tasks:
        if "ranking" in task["command"]:
            print(f"• [{task['category']}] {task['name']}")
        elif "follow" in task["command"]:
            print(f"• {task['name']}")

    if input("\n确认执行? (y/n): ").lower() != 'y':
        return

    for task in manager.tasks:
        if not manager.execute_task(task):
            print(f"任务 {task['name']} 执行失败，停止后续任务")
            break

def change_exe_path():
    global EXE_PATH, IS_PYTHON_SCRIPT
    new_path = input(f"当前download路径: {EXE_PATH}\n请输入新路径: ").strip()
    if new_path:
        EXE_PATH = new_path
        IS_PYTHON_SCRIPT = new_path.endswith('.py')
        print("路径已更新")

# === 主入口 ==========================================================
if __name__ == "__main__":
    if not os.path.exists(EXE_PATH):
        print(f"错误: 未找到执行文件 ({EXE_PATH})")
        sys.exit(1)

    manager = RankingManager()
    main_menu(manager)
