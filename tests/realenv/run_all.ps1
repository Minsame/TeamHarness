# run_all.ps1 - TeamHarness realenv 测试主执行器
# 运行所有测试套件并汇总结果
# 用法: powershell -ExecutionPolicy Bypass -File run_all.ps1

$ErrorActionPreference = "Continue"
$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$suites = @(
    @{ Name = "多用户权限边界"; Script = "test_permissions.ps1" }
    @{ Name = "并发测试";       Script = "test_concurrency.ps1" }
    @{ Name = "错误处理";       Script = "test_errors.ps1" }
    @{ Name = "数据一致性";     Script = "test_consistency.ps1" }
)

$allOutput = [System.Text.StringBuilder]::new()
$aggregatePass = 0
$aggregateFail = 0
$aggregateSkip = 0
$suiteResults = @()

Write-Host "===========================================" -ForegroundColor White
Write-Host " TeamHarness Realenv 集成测试" -ForegroundColor White
Write-Host " $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray
Write-Host "===========================================" -ForegroundColor White

foreach ($suite in $suites) {
    $scriptPath = Join-Path $baseDir $suite.Script
    Write-Host ""
    Write-Host ">>> 执行: $($suite.Name) ($($suite.Script))" -ForegroundColor Cyan

    # 每个测试脚本在独立子进程中运行，确保隔离
    $output = & powershell.exe -ExecutionPolicy Bypass -NoProfile -File $scriptPath 2>&1
    $exitCode = $LASTEXITCODE

    # 显示输出
    $output | ForEach-Object {
        Write-Host $_
        [void]$allOutput.AppendLine($_)
    }

    # 解析输出中的 PASS/FAIL/SKIP 计数
    $passCount = ($output | Select-String -Pattern '\[PASS\]').Count
    $failCount = ($output | Select-String -Pattern '\[FAIL\]').Count
    $skipCount = ($output | Select-String -Pattern '\[SKIP\]').Count

    $aggregatePass += $passCount
    $aggregateFail += $failCount
    $aggregateSkip += $skipCount

    $suiteResults += [PSCustomObject]@{
        Name  = $suite.Name
        Pass  = $passCount
        Fail  = $failCount
        Skip  = $skipCount
        Total = $passCount + $failCount + $skipCount
    }
}

# ---------------------------------------------------------------------------
# 汇总报告
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "===========================================" -ForegroundColor White
Write-Host " 汇总报告" -ForegroundColor White
Write-Host "===========================================" -ForegroundColor White
Write-Host ""
Write-Host "各套件结果:" -ForegroundColor White
$suiteResults | ForEach-Object {
    $color = if ($_.Fail -gt 0) { "Yellow" } else { "Green" }
    Write-Host ("  {0,-20} PASS={1,-3} FAIL={2,-3} SKIP={3,-3} (total={4})" -f $_.Name, $_.Pass, $_.Fail, $_.Skip, $_.Total) -ForegroundColor $color
}

$grandTotal = $aggregatePass + $aggregateFail + $aggregateSkip
Write-Host ""
Write-Host "总计: $grandTotal | PASS: $aggregatePass | FAIL: $aggregateFail | SKIP: $aggregateSkip" -ForegroundColor White

# 保存完整输出到文件
$reportPath = Join-Path $baseDir "test_report.txt"
$allOutput.ToString() | Out-File -FilePath $reportPath -Encoding utf8 -ErrorAction SilentlyContinue
Write-Host "完整报告已保存: $reportPath" -ForegroundColor DarkGray

if ($aggregateFail -gt 0) {
    Write-Host ""
    Write-Host "存在 FAIL 用例，请查看上方详情。" -ForegroundColor Yellow
    exit 1
} else {
    Write-Host ""
    Write-Host "所有用例通过。" -ForegroundColor Green
    exit 0
}
