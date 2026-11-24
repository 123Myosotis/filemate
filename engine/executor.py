# engine/executor.py
"""执行层：FileRuleEngine 负责真正移动文件；AIFileAssistant 负责把自然语言指令转交给 AI"""
from pathlib import Path  # 跨平台路径对象
import shutil  # 提供文件移动等高级文件操作
from typing import Optional, Dict, List  # 类型注解（Optional 用于可选参数）
from .rules import BaseRule  # 规则基类，用于类型标注
from ai.client import ask_llm as ask_deepseek  # 智谱对话接口（兼容旧名）
from engine.scanner import get_desktop_pdfs_content  # 桌面 PDF 内容扫描器


class FileRuleEngine:
    """原有文件规则引擎 - 保持不变"""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)  # 引擎操作的基准目录
        self.rules = []  # 已注册的规则列表，按添加顺序依次尝试

    def add_rule(self, rule: BaseRule):
        self.rules.append(rule)  # 注册一条规则（先添加的规则优先匹配）

    def apply_rules(self, files, dry_run=False):
        """对一批文件依次套用规则，命中的文件执行移动，返回移动/失败清单

        参数:
            dry_run: True 时只计算"会移动到哪"，不真正移动也不创建目录（用于方案预览）
        """
        # moved 中每项为 (原路径, 现路径)，供 UI 记录可撤回的日志
        moved, errors = [], []
        for f in files:
            f = Path(f)  # 统一转成 Path，兼容传入字符串路径的情况
            target_dir = None
            # 依次尝试每条规则，第一条命中的规则决定目标目录（命中即停止）
            for rule in self.rules:
                target_dir = rule.apply(f)
                if target_dir: break

            if target_dir:  # 有规则命中才执行移动；没有命中则保持原样
                try:
                    dst = target_dir / f.name  # 目标完整路径 = 目标目录 / 原文件名
                    if dst.exists():
                        i = 1
                        while True:
                            cand = dst.with_name(f"{dst.stem}_{i}{dst.suffix}")
                            if not cand.exists():
                                dst = cand
                                break
                            i += 1
                    if not dry_run:  # 预览模式跳过一切真实文件操作
                        target_dir.mkdir(parents=True, exist_ok=True)  # 目标目录不存在则递归创建
                        shutil.move(str(f), str(dst))  # 真正执行移动
                    moved.append((f, dst))  # 记录移动前/后路径，供撤回使用
                except Exception as e:
                    errors.append((f, str(e)))  # 单个文件失败不影响其他文件，记录后继续
        return {"moved": moved, "errors": errors}


class AIFileAssistant:
    """
    新增：AI文件助手
    整合智谱大模型与文件操作
    """

    def __init__(self, api_key: str, base_dir: Optional[Path] = None):
        self.api_key = api_key  # 智谱 API Key
        # 内置一个规则引擎实例；未指定目录时默认以桌面为操作目录
        self.file_engine = FileRuleEngine(base_dir or Path.home() / "Desktop")

    def process_command(self, user_input: str) -> str:
        """
        处理用户命令的主入口

        示例输入：
        - "帮我把桌面上的PDF整理一下"
        - "把下载文件夹里的图片分类"
        - "帮我总结这个PDF的内容"
        """
        # 1. 检测是否涉及桌面PDF操作
        if "桌面" in user_input and "PDF" in user_input:
            return self._handle_desktop_pdf(user_input)

        # 2. 其他文件操作指令
        elif any(word in user_input for word in ["整理", "分类", "移动"]):
            return self._handle_file_organization(user_input)

        # 3. 普通对话 - 直接调用AI
        else:
            return ask_deepseek(user_input, self.api_key)

    def _handle_desktop_pdf(self, user_input: str) -> str:
        """处理桌面PDF相关指令"""
        try:
            # 步骤1：扫描桌面PDF
            pdf_content = get_desktop_pdfs_content()  # 拿到桌面所有 PDF 的前 500 字摘要

            if "没有找到" in pdf_content:
                return pdf_content  # 桌面没有 PDF 时直接把提示文案返回给用户

            # 步骤2：构造AI提示词
            # 把用户原话和所有 PDF 摘要拼成一段提示词，让 AI 基于真实内容做整理分析
            ai_prompt = f"""用户说："{user_input}"

以下是桌面PDF文件的内容摘要，请帮助整理：

{pdf_content}

请以清晰的格式输出整理结果，包括：
1. 每个PDF的主要主题
2. 关键信息提取
3. 如果有多个PDF，给出它们之间的关联"""

            # 步骤3：调用大模型
            ai_response = ask_deepseek(ai_prompt, self.api_key)

            return f"📄 PDF整理结果：\n\n{ai_response}"  # 加上标题前缀后返回给 UI 展示

        except Exception as e:
            return f"❌ 处理PDF时出错：{str(e)}"  # 任何环节出错都以文案形式返回，避免界面崩溃

    def _handle_file_organization(self, user_input: str) -> str:
        """处理文件整理/分类指令"""
        # 这里可以扩展：分析用户意图，调用FileRuleEngine执行实际的文件移动
        # 或者让AI给出分类建议

        # 构造提示词：让 AI 扮演文件管理助手，输出具体可行的整理方案
        ai_prompt = f"""用户想要整理文件："{user_input}"

请分析用户的意图，并给出文件分类整理的方案建议。
注意：这是一个文件管理助手，需要提供具体可行的整理策略。"""

        return ask_deepseek(ai_prompt, self.api_key)


# 兼容旧的导入方式
__all__ = ['FileRuleEngine', 'AIFileAssistant']  # 限定 from engine.executor import * 时导出的名字