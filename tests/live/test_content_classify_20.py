# -*- coding: utf-8 -*-
"""L5：内容分类基准 —— 20 篇有标准答案的 Word。

不是只看“跑了没报错”，而是回答两个问题：
1) 20 篇里判对几篇（按 8 类 + 其他）；
2) 其中多少篇被放进「其他」，有多少是本来不该进的黑洞。

链路是产品真实入口：run_batch_content_new_docs（界面「内容识别后分类进各自新文档」）。
分类主体是规则（0 token）；低置信时才调真实模型做软标签。
因此本用例在 config.json 有 Key 时会连真实模型一起测。

默认跳过：
    set FILEMATE_LIVE=1
    python -m pytest tests/live/test_content_classify_20.py -q -s
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from docx import Document

from pipeline.stage2_classifier.doc_classifier import TYPE_LABELS

pytestmark = pytest.mark.live

OTHER = "other"

# (文件名, 期望 doc_type, 正文)
DOCS = [
    # ---------- 技术文档 ----------
    (
        "支付接口说明.docx", "technical",
        "支付服务接口说明\n"
        "接口地址：/api/v1/pay；请求方式：POST。\n"
        "请求参数：订单号 order_no、金额 amount、回调地址 notify_url。\n"
        "响应字段：code、message、trade_no、支付状态。\n"
        "实现说明：支付模块先校验签名，再写入数据库，并投递消息到缓存队列。\n"
        "配置项：生产环境需部署环境变量 APP_KEY 与商户号，依赖 redis。\n"
        "异常处理：网络超时重试三次，失败写入日志并返回错误码。",
    ),
    (
        "服务部署说明.docx", "technical",
        "服务部署说明\n"
        "运行环境：Linux + Docker，编译后生成镜像。\n"
        "配置：JVM 参数、端口、日志目录需挂载到宿主机。\n"
        "架构分为接入层、业务层、数据层，数据库采用主从。\n"
        "版本升级流程：停止实例、替换镜像、启动实例、健康检查。\n"
        "常见报错：端口占用、依赖缺失、缓存连接失败、磁盘空间不足。\n"
        "部署完成后需验证接口可用性并观察日志。",
    ),
    (
        "消息模块设计说明.docx", "technical",
        "消息模块设计说明\n"
        "本模块负责消息接收、解析、转发与落库。\n"
        "核心类：MessageHandler 负责调度，Parser 负责解析报文。\n"
        "接口字段：msg_id、sender、content、timestamp。\n"
        "参数校验失败返回 400，重试超过三次写入异常表。\n"
        "测试用例覆盖并发、断线重连与重复消费。\n"
        "日志按天切分，版本变更记录在 CHANGELOG。",
    ),
    # ---------- 报告 ----------
    (
        "实验报告-数据库课程设计.docx", "report",
        "实验报告\n"
        "课程名称：数据库原理。学号：20230101。姓名：张三。学院：计算机学院。专业：软件工程。\n"
        "一、实验目的\n掌握数据库设计与 SQL 查询优化方法。\n"
        "二、实验环境\nWindows 11、MySQL 8.0、Navicat。\n"
        "三、实验步骤\n1. 创建数据库与数据表。2. 导入测试数据。3. 编写查询语句。4. 分析执行计划。\n"
        "四、结果分析\n索引优化后查询耗时从 1.2 秒下降到 0.1 秒。\n"
        "五、实验总结\n理解了索引与执行计划的关系。指导教师：李四。",
    ),
    (
        "实训报告-小程序开发.docx", "report",
        "实训报告\n"
        "课程名称：移动应用开发实训。学号：20230202。姓名：王五。指导教师：赵六。\n"
        "一、实验目的\n完成微信小程序登录与订单模块开发。\n"
        "二、实验环境\n微信开发者工具、Node.js、云开发数据库。\n"
        "三、实验步骤\n1. 初始化项目。2. 配置云开发环境。3. 实现登录授权。4. 联调订单接口。\n"
        "四、结果分析\n登录成功率 99%，订单列表加载正常。\n"
        "五、实验总结\n掌握了小程序页面生命周期与云函数调用。",
    ),
    (
        "课程设计报告-图书管理.docx", "report",
        "课程设计报告\n"
        "课程名称：软件工程课程设计。学号：20230303。姓名：孙七。学院：信息学院。专业：计算机。\n"
        "一、实验目的\n设计并实现图书管理系统。\n"
        "二、实验内容\n完成借阅、归还、查询三个模块，编写需求说明与测试用例。\n"
        "三、实验环境\nJava 17、Spring Boot、MySQL。\n"
        "四、结果分析\n系统通过全部功能测试，平均响应时间 200 毫秒。\n"
        "五、实验总结\n实践了分层架构与数据库设计。指导教师：周八。",
    ),
    # ---------- 学术论文 ----------
    (
        "学术论文-深度学习综述.docx", "academic",
        "基于深度学习的图像识别研究综述\n"
        "摘要\n本文系统梳理了近年来图像识别领域的研究进展，分析了卷积网络与注意力机制的代表性方法。\n"
        "关键词：深度学习；图像识别；注意力机制\n"
        "引言\n随着算力提升，相关研究快速增长。本研究对比了 32 篇文献的实验结果。\n"
        "研究方法\n采用文献计量与对比实验相结合的方法。\n"
        "结论\n融合注意力机制的网络在公开数据集上表现更优。\n"
        "参考文献\n[1] He K, et al. Deep Residual Learning. CVPR, 2016.\n"
        "[2] Vaswani A, et al. Attention Is All You Need. NeurIPS, 2017.",
    ),
    (
        "期刊论文-城市交通.docx", "academic",
        "城市轨道交通客流预测方法研究\n"
        "摘要\n本文提出一种融合天气与节假日的客流预测模型，并在某市地铁线网进行验证。\n"
        "关键词：轨道交通；客流预测；时间序列\n"
        "引言\n客流预测是运营组织的基础。本研究基于三年刷卡数据展开。\n"
        "研究方法\n构建梯度提升树模型，与传统时间序列方法对比。\n"
        "结论\n模型平均绝对误差下降 12%。\n"
        "参考文献\n[1] 李明. 城市交通客流分析[J]. 交通运输研究, 2021.\n"
        "[2] 王强, et al. 短时客流预测[J]. 系统工程, 2022.",
    ),
    (
        "毕业论文-供应链管理.docx", "academic",
        "供应链金融风险评价研究\n"
        "摘要\n本文以中小企业为对象，构建供应链金融风险评价指标体系。\n"
        "关键词：供应链金融；风险评价；层次分析法\n"
        "绪论\n现有研究多聚焦单一主体，缺少对链条整体的度量。\n"
        "研究方法\n采用层次分析法确定权重，结合 120 份问卷进行实证。\n"
        "结论\n核心企业信用与交易真实性对风险影响最大。\n"
        "参考文献\n[1] 张华. 供应链金融[M]. 北京: 经济出版社, 2020.\n"
        "[2] 陈晨. 风险评价研究[J]. 管理评论, 2023.",
    ),
    # ---------- 合同 ----------
    (
        "劳务合同.docx", "contract",
        "劳务合同\n"
        "甲方：某某科技有限公司\n乙方：张某某\n"
        "第一条 服务内容\n乙方按甲方要求提供软件开发与维护服务。\n"
        "第二条 合同价款与结算\n甲方按月支付报酬，结算周期为自然月。\n"
        "第三条 违约责任\n任何一方违约，应赔偿对方因此产生的直接损失。\n"
        "第四条 争议解决\n协商不成的，提交本地仲裁委员会仲裁。\n"
        "第五条 其他\n本合同自双方签署盖章之日起生效，未尽事宜另行书面约定。",
    ),
    (
        "房屋租赁协议.docx", "contract",
        "房屋租赁协议\n"
        "甲方（出租方）：李某某\n乙方（承租方）：王某某\n"
        "第一条 租赁房屋\n甲方将位于某小区的房屋出租给乙方居住使用。\n"
        "第二条 租赁期限\n租赁期限为一年，自签署之日起计算。\n"
        "第三条 租金与结算\n月租金 3000 元，按季度结算，乙方逾期支付需承担违约责任。\n"
        "第四条 解除与争议解决\n双方协商一致可解除协议，争议提交人民法院诉讼解决。\n"
        "第五条 其他\n本协议一式两份，双方各执一份，盖章后生效。",
    ),
    (
        "采购合同-办公设备.docx", "contract",
        "采购合同\n"
        "甲方（采购方）：某某公司\n乙方（供货方）：某某设备有限公司\n"
        "第一条 标的\n甲方向乙方采购办公电脑 20 台，具体型号见附件清单。\n"
        "第二条 价款与验收\n合同总价款为人民币 12 万元，货到后 7 日内完成验收。\n"
        "第三条 交付与工期\n乙方应于 30 日内交付，逾期交付承担违约责任。\n"
        "第四条 结算方式\n验收合格后一次性结算，乙方开具发票。\n"
        "第五条 争议解决\n协商不成，提交合同签订地仲裁机构仲裁。\n"
        "第六条 生效\n本合同自双方签署盖章之日起生效。",
    ),
    # ---------- 指南教程 ----------
    (
        "软件安装指南.docx", "guide",
        "软件安装指南\n"
        "安装前请确认系统版本与磁盘空间。\n"
        "步骤 1：打开安装包，双击运行安装程序。\n"
        "步骤 2：选择安装目录，点击下一步。\n"
        "步骤 3：勾选创建桌面快捷方式，输入许可证密钥。\n"
        "步骤 4：等待进度条完成，点击完成按钮。\n"
        "注意：安装过程中不要关闭窗口。\n"
        "提示：首次启动需要联网激活，失败可重试。",
    ),
    (
        "打印机使用手册.docx", "guide",
        "打印机使用手册\n"
        "一、安装说明\n打开包装，取出打印机与电源线，连接电脑 USB 接口。\n"
        "二、使用方法\n步骤 1：打开电源开关，等待指示灯常亮。\n"
        "步骤 2：在电脑中选择打印机，点击添加设备。\n"
        "步骤 3：放入纸张，选择打印质量，点击打印。\n"
        "三、注意事项\n卡纸时请先关闭电源再取出纸张，避免损坏设备。\n"
        "提示：长期不用请取出墨盒，防止堵塞。",
    ),
    # ---------- 项目事务 ----------
    (
        "项目周报-第12周.docx", "project",
        "项目周报 第12周\n"
        "本周进度：完成登录模块联调，接口通过验收。\n"
        "里程碑：3 月 20 日完成需求评审，3 月 31 日交付测试版本。\n"
        "任务分配：张伟负责前端排期，李娜负责后端接口，王强负责测试。\n"
        "风险：第三方支付接口文档延迟，可能影响交付。\n"
        "下周计划：完成支付联调，提交迭代验收材料。\n"
        "负责人：张伟。完成情况：整体按计划推进。",
    ),
    (
        "迭代排期与需求评审.docx", "project",
        "迭代排期与需求评审\n"
        "需求范围：本次迭代包含订单查询、退款申请、消息通知三个需求。\n"
        "排期：3 月 10 日需求评审，3 月 18 日开发完成，3 月 25 日验收。\n"
        "任务拆解：每个需求拆为接口开发、页面开发、测试用例三部分。\n"
        "负责人：李娜负责订单，王强负责退款，张伟负责通知。\n"
        "风险：退款依赖财务系统接口，存在联调延迟。\n"
        "交付物：需求文档、测试报告、上线清单。",
    ),
    # ---------- 小说 / 剧本 ----------
    (
        "长篇小说-第一章.docx", "novel",
        "第一章 雨夜\n"
        "他站在屋檐下，低声说道：这雨怕是不会停了。\n"
        "她沉默了很久，忽然抬起头，眸子里带着一点笑意。\n"
        "巷子尽头的灯忽明忽暗，只见一个人影从雨里走来。\n"
        "你终于来了。他心想，嘴角微微一动。\n"
        "那人没有回答，只是颤抖着从怀里取出一封信。\n"
        "从前他总以为，有些事一辈子都不会再提起。",
    ),
    (
        "剧本-咖啡馆分镜.docx", "script",
        "剧本：咖啡馆的午后\n"
        "第一幕 内景 咖啡馆 日\n"
        "旁白：午后的阳光落在窗边，杯子里还冒着热气。\n"
        "场景：靠窗座位，桌上放着两杯咖啡。\n"
        "角色：主角林夏，反派陈默。\n"
        "台词：\n林夏：你终于来了。\n陈默：有些话，我只说一次。\n"
        "分镜 1：特写咖啡杯，镜头缓慢推近。\n"
        "分镜 2：两人对视，剧情进入第二幕。\n"
        "第二幕 外景 街道 夜\n旁白：雨开始下了。",
    ),
    # ---------- 真的不属于八类（其他） ----------
    (
        "会议纪要-产品评审.docx", OTHER,
        "会议纪要\n"
        "时间：2026年3月5日下午三点\n"
        "地点：三楼小会议室\n"
        "参会人：张伟、李娜、王强、赵敏\n"
        "记录人：赵敏\n"
        "会议内容：大家围绕近期工作进行了沟通，就协作方式交换了意见。\n"
        "会议决定：保持每周一次沟通，具体时间另行通知。\n"
        "备注：本次会议未形成书面材料。",
    ),
    (
        "团建活动通知.docx", OTHER,
        "关于秋季团建活动的通知\n"
        "各位同事：\n"
        "为增进了解、放松身心，公司定于十月十八日组织秋季团建活动。\n"
        "活动地点为城郊生态园，集合时间为上午八点，统一乘车前往。\n"
        "请大家合理安排时间，务必准时参加。\n"
        "如有特殊情况不能参加，请提前告知行政部。\n"
        "特此通知。\n行政部\n2026年9月28日",
    ),
]


def _need_live() -> None:
    if os.environ.get("FILEMATE_LIVE") != "1":
        pytest.skip("未开启真实模型测试：设置 FILEMATE_LIVE=1")


def _write_docs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, _expected, text in DOCS:
        doc = Document()
        for line in text.split("\n"):
            doc.add_paragraph(line)
        doc.save(str(root / name))


def _isolate_db(tmp_path, monkeypatch) -> None:
    import db.database as db_mod

    db_path = tmp_path / "live.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    try:
        import infra.content_tags as ct

        monkeypatch.setattr(ct, "DB_PATH", db_path)
    except Exception:  # noqa: BLE001
        pass


def test_content_classify_20(tmp_path, monkeypatch):
    _need_live()
    _isolate_db(tmp_path, monkeypatch)

    ws = tmp_path / "语料库"
    _write_docs(ws)

    from pipeline.content_split.batch_new_docs import run_batch_content_new_docs

    out = run_batch_content_new_docs("内容识别后分类进各自新文档", ws)
    assert out.get("ok") is True, out

    rows = {Path(str(r.get("src") or "")).name: r
            for r in (out.get("classifications") or [])}
    assert len(rows) == len(DOCS), (
        "源文件数对不上：预期 %d，实际 %d；跳过=%r"
        % (len(DOCS), len(rows),
           [r.get("skip_reason") for r in rows.values() if r.get("status") == "跳过"]))

    correct = 0
    other_n = 0
    false_other = []
    errors = []
    per_class_total = Counter()
    per_class_hit = Counter()
    lines = []

    for name, expected, _text in DOCS:
        row = rows.get(name)
        assert row is not None, "没有处理结果：%s" % name
        assert row.get("status") in ("已分类", "已重算"), (
            "%s 未完成分类：status=%r skip=%r"
            % (name, row.get("status"), row.get("skip_reason")))

        got = str(row.get("doc_type") or "")
        per_class_total[expected] += 1
        if got == expected:
            correct += 1
            per_class_hit[expected] += 1
        else:
            errors.append((name, expected, got, row))
        if got == OTHER:
            other_n += 1
            if expected != OTHER:
                false_other.append((name, expected, row))

        # 产物落盘也要对：dest 指向实际复制出的新文档
        dest = str(row.get("dest") or "").strip("`")
        assert dest and (ws / dest).is_file(), "%s 的产物不存在：%r" % (name, dest)

        lines.append(
            "  %-28s 预期=%-9s 实际=%-9s %s | source=%s | top2=%s"
            % (name, TYPE_LABELS.get(expected, expected),
               TYPE_LABELS.get(got, got),
               "✓" if got == expected else "✗",
               row.get("source") or "-", row.get("top2") or "-")
        )

    total = len(DOCS)
    print("\n[内容分类基准] %d 篇" % total)
    print("正确：%d/%d（%.1f%%）" % (correct, total, correct * 100.0 / total))
    print("进「其他」：%d 篇（其中本不该进 %d 篇）" % (other_n, len(false_other)))
    print("按类召回：")
    for dtype in ("technical", "report", "academic", "contract",
                  "guide", "project", "novel", "script", OTHER):
        if per_class_total[dtype]:
            print("  %-9s %d/%d"
                  % (TYPE_LABELS.get(dtype, dtype),
                     per_class_hit[dtype], per_class_total[dtype]))
    print("逐篇：")
    print("\n".join(lines))
    if errors:
        print("错分明细：")
        for name, expected, got, row in errors:
            print("  %s 预期=%s 实际=%s low_confidence=%r source=%s"
                  % (name, TYPE_LABELS.get(expected, expected),
                     TYPE_LABELS.get(got, got),
                     row.get("low_confidence"), row.get("source")))

    # 干净语料上的底线；低于此值说明规则/软标签出了真问题
    assert correct >= 16, "20 篇只对 %d 篇，低于底线 16" % correct
    assert len(false_other) <= 2, "有 %d 篇被误丢进「其他」" % len(false_other)
