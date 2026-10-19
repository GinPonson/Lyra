"""账本领域错误。

与 TypeScript 版 ``errors.ts`` 对应。导入路径上的错误一律**向上抛出并中止整个
文件**，不允许跳过单行——半导入状态比拒绝整份文件更难排查。
"""

from __future__ import annotations


class IngestError(Exception):
    """导入解析或校验失败。

    错误消息面向服务端诊断，不含完整文件内容；调用方必须中止当前文件，
    不得跳过出错的行。
    """
