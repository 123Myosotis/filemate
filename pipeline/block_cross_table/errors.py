"""跨表阶段错误。错误码字符串与契约 cross_table.error_codes 对齐；本模块不实现业务逻辑。"""


class CrossTableError(Exception):
    """携带契约错误码的跨表失败。"""

    def __init__(self, code: str, message: str = "", details=None):
        self.code = code
        self.message = message
        self.details = details if details is not None else {}
        super().__init__(code if not message else f"{code}: {message}")
