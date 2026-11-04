"""新链路拼接：按对齐列键并排，无对齐则上下相接。不写复核、不落库。"""

from pipeline.block_stitch.normalize import normalize_by_source_column
from pipeline.block_stitch.stitch import StitchError, stitch

__all__ = ["StitchError", "normalize_by_source_column", "stitch"]
