import '../api/lyra_api_error.dart';

/// UI 错误边界：协议码用于选择可操作的提示，原始正文与诊断信息不进入界面。
/// 调用方可以提供当前任务的兜底文案；未知异常也不会调用 toString()。
String lyraErrorMessage(Object? error, {String fallback = '暂时无法完成操作，请稍后重试。'}) {
  if (error is! LyraApiError) return fallback;
  return switch (error.code) {
    'CLIENT_VERSION_INCOMPATIBLE' => '请更新 Lyra 后重试。',
    'AUTH_REQUIRED' => '请先登录 Lyra。',
    'AUTH_SESSION_CHANGED' => '登录状态已变化，请重新打开当前页面。',
    'INVITE_CODE_REQUIRED' => '首次使用需要邀请码，请填写后重试。',
    'INVITE_CODE_INVALID' => '邀请码无效或已被使用，请确认后重试。',
    'DEVICE_CREDENTIAL_INVALID' ||
    'DEVICE_CREDENTIAL_EXPIRED' ||
    'DEVICE_CREDENTIAL_REUSED' ||
    'DEVICE_REVOKED' => '登录已失效，请重新登录。',
    'ACCOUNT_MAPPING_REQUIRED' => '请选择文件对应的证券账户。',
    'ACCOUNT_ALREADY_EXISTS' => '该证券账户已存在，请返回并选择已有账户。',
    'ACCOUNT_SCOPE_REQUIRED' => '请先选择要查看的证券账户。',
    'IMPORT_UNSUPPORTED_FORMAT' => '请选择 Schwab 导出的 CSV 或 Excel 文件。',
    'IMPORT_UNKNOWN_HEADERS' => '未找到所需的交易列，请重新从 Schwab 导出文件。',
    'IMPORT_AMBIGUOUS_SHEET' => '文件中有多个交易工作表，请选择只包含一份交易记录的文件。',
    'IMPORT_VALIDATION_FAILED' => '文件中有无法识别的交易记录，请检查内容或重新导出后再试。',
    'IMPORT_PREVIEW_STALE' => '当前预览已失效，请重新预览后再确认导入。',
    'IMPORT_STATE_CONFLICT' => '当前导入状态已变化，请重新预览后再试。',
    'LEDGER_VERSION_CONFLICT' => '账本已有更新，请重新预览后再确认导入。',
    'LEDGER_REBUILD_FAILED' => '部分交易无法与现有持仓对应，请核对开仓和平仓记录后再试。',
    'IDEMPOTENCY_REQUEST_IN_PROGRESS' => '上一项操作仍在处理中，请稍后重试。',
    'INVALID_ARGUMENT' => '请检查填写的内容或所选条件后重试。',
    'AGENT_UNAVAILABLE' => '助手暂不可用，请稍后重试。',
    'AGENT_SESSION_STATE_CONFLICT' => '对话状态已变化，请重新打开对话后重试。',
    'AGENT_TURN_FAILED' || 'AGENT_TOOL_EXECUTION_FAILED' => '本次分析未完成，请重试。',
    'AGENT_TURN_CANCELLED' || 'AGENT_TOOL_CANCELLED' => '本次分析已停止。',
    'AGENT_TOOL_FORBIDDEN' => '暂时无法完成这项分析，请尝试其他问题。',
    _ when error.statusCode == 401 => '登录已失效，请重新登录。',
    _ when error.statusCode == 403 => '当前账号暂时无法完成此操作。',
    _ when error.statusCode == 429 => '操作较频繁，请稍后重试。',
    _ when error.statusCode == 0 && error.retryable => '暂时无法完成操作，请检查网络后重试。',
    _ => fallback,
  };
}
