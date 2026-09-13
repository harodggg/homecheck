"""homecheck —— 只读的本机环境体检工具。

安全约束：包内除 ``apply``（S5，尚未实现）外，任何模块都不得改动文件系统。
"""

from .scan import ScanIssue, ScanResult, scan_directory

__version__ = "0.1.0"

__all__ = ["ScanIssue", "ScanResult", "scan_directory", "__version__"]
