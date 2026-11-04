# engine/rules.py
"""文件分类规则库：每条规则独立判断一个文件应该移动到哪个目标文件夹"""
from pathlib import Path  # 跨平台路径对象，规则匹配与目标路径拼装的载体
from datetime import datetime  # 把时间戳转成日期（DateRule 用）
import time  # 获取当前时间戳（RecentFilesRule 用）

class BaseRule:
    """规则基类：所有规则都要实现 apply()；命中返回目标文件夹 Path，不命中返回 None"""
    def apply(self, file_path: Path) -> Path:
        raise NotImplementedError  # 基类不实现具体逻辑，强制子类重写

class ExtensionRule(BaseRule):
    """按扩展名分类：例如 .pdf → 文档/、.jpg → 图片/"""
    def __init__(self, mapping: dict):
        self.mapping = mapping  # 映射表，形如 {".pdf": "文档", ".jpg": "图片"}
    def apply(self, file_path: Path) -> Path:
        ext = file_path.suffix.lower()  # 取扩展名并转小写，兼容 ".PDF"/".Pdf" 等写法
        if ext in self.mapping:
            return file_path.parent / self.mapping[ext]  # 目标目录 = 文件当前所在目录 / 分类文件夹
        return None  # 扩展名不在映射表中 → 本规则不适用

class KeywordRule(BaseRule):
    """按文件名关键词分类：文件名包含某关键词就归入对应文件夹"""
    def __init__(self, mapping: dict):
        self.mapping = mapping  # 映射表，形如 {"合同": "合同类"}
    def apply(self, file_path: Path) -> Path:
        name = file_path.name  # 只匹配文件名（含扩展名），不看完整路径
        for kw, folder in self.mapping.items():
            if kw in name:  # 命中第一个关键词即返回（按映射表遍历顺序）
                return file_path.parent / folder
        return None  # 没有任何关键词命中

class DateRule(BaseRule):
    """按文件修改日期归档：默认归档到「年/月」两层文件夹"""
    def __init__(self, date_format="%Y/%m"):
        self.date_format = date_format  # strftime 格式串；其中的 "/" 会自然形成多级目录
    def apply(self, file_path: Path) -> Path:
        mtime = file_path.stat().st_mtime  # 读取文件最后修改时间（Unix 时间戳）
        dt = datetime.fromtimestamp(mtime)  # 时间戳转成本地日期时间对象
        return file_path.parent / dt.strftime(self.date_format)  # 例如得到 2026/10

class ExactNameRule(BaseRule):
    """按完整文件名精确匹配（AI 点名移动某几个文件时使用，避免子串误伤）"""
    def __init__(self, names: set, target_folder: str):
        self.names = set(names)  # 允许的完整文件名集合（含扩展名）
        self.target_folder = target_folder
    def apply(self, file_path: Path) -> Path:
        if file_path.name in self.names:  # 全等比较，"a.docx" 不会误中 "ba.docx"
            return file_path.parent / self.target_folder
        return None

class RecentFilesRule(BaseRule):
    """最近 N 天内修改过的文件 → 目标文件夹（如"上周修改过的文件备份"）"""
    def __init__(self, days: int, target_folder: str):
        self.days = days  # 时间窗口长度（天）
        self.target_folder = target_folder  # 命中的文件要移动到的目标文件夹名
    def apply(self, file_path: Path) -> Path:
        mtime = file_path.stat().st_mtime  # 文件最后修改时间戳
        # 86400 是一天的秒数；当前时间与修改时间的差不超过 N 天，即视为"最近修改过"
        if (time.time() - mtime) <= self.days * 86400:
            return file_path.parent / self.target_folder
        return None  # 超出时间窗口，本规则不匹配