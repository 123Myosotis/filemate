# 伪代码：在 file_panel.py 的“整理”按钮回调中
from engine.executor import FileRuleEngine
from engine.rules import ExtensionRule, KeywordRule, DateRule


def on_sort_clicked(selected_files):
    engine = FileRuleEngine(".")
    engine.add_rule(ExtensionRule({'.pdf': '文档', '.jpg': '图片'}))
    engine.add_rule(KeywordRule({'合同': '合同类'}))
    engine.add_rule(DateRule("%Y/%m"))

    result = engine.apply_rules(selected_files)
    print("整理完成", result)