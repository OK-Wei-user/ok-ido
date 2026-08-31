#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_session_files.py
会话文件列表过滤与排序单元测试
- 空文件过滤(含sync_status守卫: PENDING文件保留, SYNCED空文件过滤)
- 核心交付文件优先排序
- 过程文件识别(脚本/日志/检查类)
- 交付文件路径提取(含过程文件智能过滤)

F2-2架构变更: 文件展示策略已从SessionService抽离到FilePresentationService,
测试同步迁移至直接测试FilePresentationService接口。
"""
from app.application.services.file_presentation_service import FilePresentationService
from app.domain.models.file import File
from unittest.mock import MagicMock


def _make_file(filename: str, size: int, filepath: str = "", sync_status: str = "SYNCED") -> File:
    """构造测试用File对象"""
    return File(
        filename=filename,
        filepath=filepath or f"/home/ubuntu/data/{filename}",
        size=size,
        sync_status=sync_status,
    )


def _make_service() -> FilePresentationService:
    """构造默认配置的FilePresentationService实例"""
    return FilePresentationService()


class TestFilterEmptyFiles:
    """filter_empty_files静态方法测试 — 含sync_status守卫"""

    def test_filters_zero_size_synced_files(self):
        """SYNCED状态且size为0的文件应被过滤"""
        files = [
            _make_file("report.xlsx", 1024),
            _make_file("empty.txt", 0, sync_status="SYNCED"),
            _make_file("chart.png", 512),
        ]
        result = FilePresentationService.filter_empty_files(files)
        assert len(result) == 2
        assert all(f.size > 0 for f in result)

    def test_keeps_pending_files_with_zero_size(self):
        """PENDING状态且size为0的文件应保留(可能尚未同步完成)"""
        files = [
            _make_file("report.xlsx", 1024),
            _make_file("pending.txt", 0, sync_status="PENDING"),
        ]
        result = FilePresentationService.filter_empty_files(files)
        assert len(result) == 2
        assert any(f.filename == "pending.txt" for f in result)

    def test_filters_failed_files_with_zero_size(self):
        """FAILED状态且size为0的文件应被过滤(同步失败的空文件)"""
        files = [
            _make_file("ok.xlsx", 100),
            _make_file("failed.txt", 0, sync_status="FAILED"),
        ]
        result = FilePresentationService.filter_empty_files(files)
        assert len(result) == 1
        assert result[0].filename == "ok.xlsx"

    def test_keeps_all_non_empty_files(self):
        """size>0的文件应全部保留(无论同步状态)"""
        files = [
            _make_file("a.xlsx", 100, sync_status="SYNCED"),
            _make_file("b.csv", 200, sync_status="PENDING"),
            _make_file("c.png", 300, sync_status="FAILED"),
        ]
        result = FilePresentationService.filter_empty_files(files)
        assert len(result) == 3

    def test_empty_list_returns_empty(self):
        """空列表应返回空列表"""
        assert FilePresentationService.filter_empty_files([]) == []

    def test_all_synced_empty_files_returns_empty(self):
        """全部为SYNCED空文件时应返回空列表"""
        files = [
            _make_file("empty1.txt", 0, sync_status="SYNCED"),
            _make_file("empty2.log", 0, sync_status="SYNCED"),
        ]
        assert FilePresentationService.filter_empty_files(files) == []

    def test_filters_all_zero_size_variants(self):
        """SYNCED状态size为0的文件应被过滤(无论文件类型)"""
        files = [
            _make_file("empty.xlsx", 0, sync_status="SYNCED"),
            _make_file("empty.png", 0, sync_status="SYNCED"),
            _make_file("valid.txt", 10),
        ]
        result = FilePresentationService.filter_empty_files(files)
        assert len(result) == 1
        assert result[0].filename == "valid.txt"


class TestSortFilesByPriority:
    """sort_files_by_priority方法测试"""

    def test_core_delivery_files_rank_first(self):
        """xlsx等核心交付文件应排在最前"""
        files = [
            _make_file("script.py", 100),
            _make_file("debug.log", 50),
            _make_file("report.xlsx", 2000),
            _make_file("notes.txt", 300),
        ]
        result = _make_service().sort_files_by_priority(files)
        assert result[0].filename == "report.xlsx"

    def test_priority_order_xlsx_before_png_before_txt_before_py_before_log(self):
        """完整优先级链: xlsx > png > txt > py > log"""
        files = [
            _make_file("debug.log", 50),
            _make_file("script.py", 100),
            _make_file("notes.txt", 300),
            _make_file("chart.png", 500),
            _make_file("report.xlsx", 2000),
        ]
        result = _make_service().sort_files_by_priority(files)
        extensions = [f.filename.rsplit(".", 1)[-1] for f in result]
        assert extensions == ["xlsx", "png", "txt", "py", "log"]

    def test_unknown_extension_uses_default_priority(self):
        """未知扩展名使用默认优先级(40),排在已知核心类型之后、过程文件之前"""
        files = [
            _make_file("script.py", 100),
            _make_file("data.xyz", 200),
            _make_file("report.xlsx", 2000),
        ]
        result = _make_service().sort_files_by_priority(files)
        assert result[0].filename == "report.xlsx"
        assert result[1].filename == "data.xyz"
        assert result[2].filename == "script.py"

    def test_empty_list_returns_empty(self):
        """空列表应返回空列表"""
        assert _make_service().sort_files_by_priority([]) == []

    def test_single_file_returns_unchanged(self):
        """单元素列表应原样返回"""
        files = [_make_file("only.xlsx", 100)]
        result = _make_service().sort_files_by_priority(files)
        assert len(result) == 1
        assert result[0].filename == "only.xlsx"

    def test_same_priority_stable_sort(self):
        """同优先级文件保持原始相对顺序(stable sort)"""
        files = [
            _make_file("a.xlsx", 100),
            _make_file("b.xlsx", 200),
            _make_file("c.xlsx", 300),
        ]
        result = _make_service().sort_files_by_priority(files)
        assert [f.filename for f in result] == ["a.xlsx", "b.xlsx", "c.xlsx"]

    def test_case_insensitive_extension_matching(self):
        """扩展名大小写不敏感"""
        files = [
            _make_file("script.PY", 100),
            _make_file("report.XLSX", 2000),
        ]
        result = _make_service().sort_files_by_priority(files)
        assert result[0].filename == "report.XLSX"
        assert result[1].filename == "script.PY"

    def test_fallback_to_filepath_when_filename_empty(self):
        """filename为空时使用filepath提取扩展名"""
        files = [
            File(filename="", filepath="/home/ubuntu/data/report.xlsx", size=100),
            File(filename="", filepath="/home/ubuntu/data/script.py", size=50),
        ]
        result = _make_service().sort_files_by_priority(files)
        assert "report.xlsx" in result[0].filepath


class TestGetSessionFilesDeliveryPriority:
    """present_files交付文件优先+全量展示测试"""

    def test_delivered_files_appear_before_non_delivered(self):
        """交付文件应排在非交付文件前面"""
        delivered = _make_file("report.xlsx", 2000, filepath="/data/report.xlsx")
        process = _make_file("debug.log", 50, filepath="/data/debug.log")
        session = MagicMock()
        session.files = [process, delivered]  # 故意把交付文件放后面

        svc = _make_service()
        all_files = svc.deduplicate_files(svc.filter_empty_files(list(session.files)))

        # 模拟extract_delivered_file_paths返回交付文件路径
        from unittest.mock import patch
        with patch.object(svc, "extract_delivered_file_paths", return_value={"/data/report.xlsx"}):
            result = svc.present_files(session, all_files)

        assert result[0].filepath == "/data/report.xlsx"
        assert result[1].filepath == "/data/debug.log"

    def test_all_files_shown_when_delivered_paths_exist(self):
        """有交付路径时也应展示全部文件(交付+非交付)"""
        delivered = _make_file("report.xlsx", 2000, filepath="/data/report.xlsx")
        process1 = _make_file("script.py", 100, filepath="/data/script.py")
        process2 = _make_file("debug.log", 50, filepath="/data/debug.log")
        session = MagicMock()
        session.files = [delivered, process1, process2]

        svc = _make_service()
        all_files = svc.deduplicate_files(svc.filter_empty_files(list(session.files)))

        # 模拟extract_delivered_file_paths返回交付文件路径
        from unittest.mock import patch
        with patch.object(svc, "extract_delivered_file_paths", return_value={"/data/report.xlsx"}):
            result = svc.present_files(session, all_files)

        # 全部3个文件都应出现
        assert len(result) == 3
        # 交付文件排第一
        assert result[0].filepath == "/data/report.xlsx"

    def test_no_delivered_paths_returns_all_sorted(self):
        """无交付路径时返回全部文件(按优先级排序)"""
        file_a = _make_file("script.py", 100, filepath="/data/script.py")
        file_b = _make_file("report.xlsx", 2000, filepath="/data/report.xlsx")
        session = MagicMock()
        session.files = [file_a, file_b]

        svc = _make_service()
        all_files = svc.deduplicate_files(svc.filter_empty_files(list(session.files)))

        # 模拟extract_delivered_file_paths返回空集合
        from unittest.mock import patch
        with patch.object(svc, "extract_delivered_file_paths", return_value=set()):
            result = svc.present_files(session, all_files)

        assert len(result) == 2
        # xlsx优先级高于py
        assert result[0].filepath == "/data/report.xlsx"
        assert result[1].filepath == "/data/script.py"


class TestIsLikelyProcessFile:
    """is_likely_process_file方法测试 — 过程文件智能识别"""

    def test_log_file_is_process_file(self):
        """日志文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/debug.log")
        assert svc.is_likely_process_file("/home/ubuntu/data/run.log")

    def test_analysis_script_is_process_file(self):
        """分析类脚本(.py/.js)应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/analysis.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/detailed_analysis.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/generate_excel_report.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/create_report.js")
        assert svc.is_likely_process_file("/home/ubuntu/data/comprehensive_analysis.py")

    def test_check_txt_is_process_file(self):
        """检查类txt文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/syntax_check.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/cols_check.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/final_check.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/date_check.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/data_check_v4.txt")

    def test_preview_txt_is_process_file(self):
        """预览类txt文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/data_preview.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/column_overview_v4.txt")

    def test_section_txt_is_process_file(self):
        """分段txt文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/analysis_section_00.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/section1_overview.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/section3_category.txt")

    def test_versioned_txt_is_process_file(self):
        """带版本号后缀的txt文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/data_v1.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/report_v3.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/summary_v5.txt")

    def test_clean_report_txt_is_process_file(self):
        """清洗报告txt文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/clean_report.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/extra_clean.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/report_data.txt")

    def test_final_delivery_xlsx_not_process_file(self):
        """最终交付xlsx不应识别为过程文件"""
        svc = _make_service()
        assert not svc.is_likely_process_file(
            "/home/ubuntu/data/入库明细_202601_202606.xlsx"
        )
        assert not svc.is_likely_process_file("/home/ubuntu/data/final_data.xlsx")

    def test_final_delivery_docx_not_process_file(self):
        """最终交付docx不应识别为过程文件"""
        svc = _make_service()
        assert not svc.is_likely_process_file(
            "/home/ubuntu/data/2026年上半年入库数据分析报告.docx"
        )
        assert not svc.is_likely_process_file("/home/ubuntu/data/final_report.docx")

    def test_final_delivery_txt_not_process_file(self):
        """正常命名的txt交付物不应识别为过程文件(保守过滤,避免误杀)"""
        svc = _make_service()
        # 用户明确要求生成的txt报告不应被误杀
        assert not svc.is_likely_process_file("/home/ubuntu/data/入库分析报告.txt")
        assert not svc.is_likely_process_file("/home/ubuntu/data/final_result.txt")
        assert not svc.is_likely_process_file("/home/ubuntu/data/总结.txt")

    def test_final_delivery_png_not_process_file(self):
        """图表类交付物不应识别为过程文件"""
        svc = _make_service()
        assert not svc.is_likely_process_file("/home/ubuntu/data/trend_chart.png")
        assert not svc.is_likely_process_file("/home/ubuntu/data/月度对比.jpg")

    def test_empty_filepath_returns_false(self):
        """空路径不应识别为过程文件"""
        svc = _make_service()
        assert not svc.is_likely_process_file("")
        assert not svc.is_likely_process_file(None)

    def test_unknown_extension_not_process_file(self):
        """未知扩展名不应识别为过程文件"""
        svc = _make_service()
        assert not svc.is_likely_process_file("/home/ubuntu/data/data.unknown")
        assert not svc.is_likely_process_file("/home/ubuntu/data/file.xyz")

    # === 11优化: 扩展过程文件模式测试(覆盖数据分析场景) ===

    def test_analyze_script_is_process_file(self):
        """analyze前缀的分析脚本应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/analyze_data.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/analyze_short.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/analyze_v2.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/analyze_final.py")

    def test_gen_script_is_process_file(self):
        """gen/generate开头的生成脚本应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/gen_report.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/gen_charts.py")

    def test_summary_script_is_process_file(self):
        """summary命名的汇总脚本应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/summary_data.py")
        assert svc.is_likely_process_file("/home/ubuntu/data/build_summary.py")

    def test_data_summary_txt_is_process_file(self):
        """data_summary.txt等汇总类过程文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/data_summary.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/step_summary.txt")

    def test_analysis_result_txt_is_process_file(self):
        """analysis_result.txt等分析结果过程文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/analysis_result.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/data_analysis_report.txt")

    def test_out_md5_txt_is_process_file(self):
        """输出类/md5类txt过程文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/analysis_out.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/data_md5.txt")

    def test_short_txt_is_process_file(self):
        """简写版txt过程文件应识别为过程文件"""
        svc = _make_service()
        assert svc.is_likely_process_file("/home/ubuntu/data/analyze_short.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/data_short.txt")

    # === F10-8批次17: lines切片文件过滤(read_file 行切片 + sed -n 'N,Mp' 输出) ===

    def test_lines_slice_txt_is_process_file(self):
        """lines切片文件应识别为过程文件

        设计动机: LLM 用 read_file 读取大文件后,可能用 write_file 创建
        lines91_95.txt / lines_91_95.txt 等切片文件暂存内容,
        误写入 attachments 污染交付列表。该模式应被识别为过程文件剔除。
        """
        svc = _make_service()
        # linesN_M.txt 模式(无下划线)
        assert svc.is_likely_process_file("/home/ubuntu/lines91_95.txt")
        assert svc.is_likely_process_file("/home/ubuntu/data/lines100_200.txt")
        # lines_N_M.txt 模式(有下划线)
        assert svc.is_likely_process_file("/home/ubuntu/lines_91_95.txt")
        # lines.txt 模式(纯 lines 命名)
        assert svc.is_likely_process_file("/home/ubuntu/data/lines.txt")


class TestExtractDeliveredFilePathsWithFilter:
    """extract_delivered_file_paths过程文件过滤测试"""

    def test_filters_process_files_from_delivered_paths(self):
        """交付路径中的过程文件应被过滤,仅保留真正交付物"""
        from app.domain.models.event import MessageEvent

        # 模拟最终消息attachments包含交付物+过程文件
        final_xlsx = _make_file("入库明细.xlsx", 2000, filepath="/data/入库明细.xlsx")
        final_docx = _make_file("分析报告.docx", 5000, filepath="/data/分析报告.docx")
        process_check = _make_file("syntax_check.txt", 100, filepath="/data/syntax_check.txt")
        process_script = _make_file("analysis.py", 300, filepath="/data/analysis.py")
        process_section = _make_file("section1.txt", 200, filepath="/data/section1.txt")

        final_event = MessageEvent(
            role="assistant",
            message="最终回复",
            attachments=[final_xlsx, final_docx, process_check, process_script, process_section],
            is_streaming=False,
            is_final=True,
        )

        session = MagicMock()
        session.events = [final_event]

        result = _make_service().extract_delivered_file_paths(session)
        # 仅保留xlsx和docx, 过滤掉txt/py过程文件
        assert "/data/入库明细.xlsx" in result
        assert "/data/分析报告.docx" in result
        assert "/data/syntax_check.txt" not in result
        assert "/data/analysis.py" not in result
        assert "/data/section1.txt" not in result

    def test_keeps_all_when_only_deliveries(self):
        """全部为交付物时应全部保留"""
        from app.domain.models.event import MessageEvent

        final_xlsx = _make_file("data.xlsx", 2000, filepath="/data/data.xlsx")
        final_docx = _make_file("report.docx", 5000, filepath="/data/report.docx")

        final_event = MessageEvent(
            role="assistant",
            message="最终回复",
            attachments=[final_xlsx, final_docx],
            is_streaming=False,
            is_final=True,
        )

        session = MagicMock()
        session.events = [final_event]

        result = _make_service().extract_delivered_file_paths(session)
        assert len(result) == 2
        assert "/data/data.xlsx" in result
        assert "/data/report.docx" in result

    def test_empty_when_no_final_message(self):
        """无最终消息时应返回空集合"""
        session = MagicMock()
        session.events = []
        result = _make_service().extract_delivered_file_paths(session)
        assert result == set()


class TestFilePresentationConfigDriven:
    """F2-3配置外置测试: 验证模式可通过config调整"""

    def test_custom_log_extension_treated_as_process_file(self):
        """自定义log扩展名(如.out)应被识别为过程文件"""
        from app.domain.models.app_config import FilePresentationConfig
        cfg = FilePresentationConfig(log_extensions=[".out"])
        svc = FilePresentationService(config=cfg)
        assert svc.is_likely_process_file("/data/build.out")
        # 默认.log不在自定义配置中,不再被识别
        assert not svc.is_likely_process_file("/data/debug.log")

    def test_custom_script_pattern_treated_as_process_file(self):
        """自定义脚本模式应被识别为过程文件"""
        from app.domain.models.app_config import FilePresentationConfig
        cfg = FilePresentationConfig(
            script_extensions=[".r"],
            script_name_patterns=["forecast", "predict"],
        )
        svc = FilePresentationService(config=cfg)
        assert svc.is_likely_process_file("/data/forecast_sales.r")
        assert svc.is_likely_process_file("/data/predict_demand.r")
        # 默认.py模式不在自定义配置中,不再被识别
        assert not svc.is_likely_process_file("/data/analysis.py")

    def test_custom_file_type_priority(self):
        """自定义文件类型优先级应生效"""
        from app.domain.models.app_config import FilePresentationConfig
        cfg = FilePresentationConfig(
            file_type_priority={".r": 200, ".xlsx": 100},
            default_file_priority=10,
        )
        svc = FilePresentationService(config=cfg)
        files = [
            _make_file("a.xlsx", 100),
            _make_file("b.r", 50),
        ]
        result = svc.sort_files_by_priority(files)
        # .r优先级200 > .xlsx优先级100
        assert result[0].filename == "b.r"
        assert result[1].filename == "a.xlsx"

    def test_default_config_matches_legacy_hardcoded_values(self):
        """默认配置应与原SessionService硬编码值完全一致(向后兼容)"""
        svc = _make_service()
        # 关键回归点: 原硬编码的优先级与模式应保留
        assert svc._config.file_type_priority[".xlsx"] == 100
        assert svc._config.file_type_priority[".log"] == 20
        assert svc._config.default_file_priority == 40
        assert ".log" in svc._config.log_extensions
        assert ".py" in svc._config.script_extensions
        assert "analysis" in svc._config.script_name_patterns
        assert "_check" in svc._config.text_process_name_patterns
        assert "_v4" in svc._config.text_process_name_patterns
