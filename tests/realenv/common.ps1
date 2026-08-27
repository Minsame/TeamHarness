# common.ps1 - TeamHarness realenv 测试共享工具
# 被 setup.ps1 / test_*.ps1 / run_all.ps1 dot-source 引入
# 兼容 Windows PowerShell 5.1（不用 try/catch 表达式）

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$script:BaseUrl = "http://localhost:8080"

# 结果收集器
$script:TestResults = [System.Collections.ArrayList]::new()
$script:PassCount = 0
$script:FailCount = 0
$script:SkipCount = 0
$script:SuiteName = "default"

# ---------------------------------------------------------------------------
# HTTP 封装：捕获 StatusCode（含 4xx/5xx 错误体），不抛异常
# ---------------------------------------------------------------------------
function Invoke-Api {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$Method = "GET",
        [object]$Body = $null,
        [hashtable]$Headers = @{},
        [int]$TimeoutSec = 15
    )
    $uri = "$script:BaseUrl$Path"

    $statusCode = 0
    $respBody = ""
    $errMsg = $null
    $jsonVal = $null

    try {
        $params = @{
            Uri             = $uri
            Method          = $Method
            Headers         = $Headers
            TimeoutSec      = $TimeoutSec
            UseBasicParsing = $true
            ErrorAction     = "Stop"
        }
        if ($null -ne $Body) {
            $params.Body = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Compress -Depth 10 }
            $params.ContentType = "application/json"
        }
        $response = Invoke-WebRequest @params
        $statusCode = [int]$response.StatusCode
        $respBody = $response.Content
    } catch {
        if ($_.Exception.Response) {
            try {
                $statusCode = [int]$_.Exception.Response.StatusCode
                $stream = $_.Exception.Response.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $respBody = $reader.ReadToEnd()
                $reader.Close()
            } catch {}
        }
        $errMsg = $_.Exception.Message
    }

    # 解析 JSON（独立 try/catch，不用表达式形式）
    if ($respBody) {
        try {
            $jsonVal = $respBody | ConvertFrom-Json
        } catch {
            $jsonVal = $null
        }
    }

    return @{
        StatusCode = $statusCode
        Body       = $respBody
        Json       = $jsonVal
        Error      = $errMsg
    }
}

# ---------------------------------------------------------------------------
# 颁发 API Key（POST /v1/auth/apikey）
# ---------------------------------------------------------------------------
function Issue-ApiKey {
    param([string]$MemberId, [string]$AgentId)
    $body = @{ member_id = $MemberId; agent_id = $AgentId }
    $r = Invoke-Api -Path "/v1/auth/apikey" -Method "POST" -Body $body
    if ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.api_key) {
        return $r.Json.api_key
    }
    return $null
}

# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------
function Set-SuiteName {
    param([string]$Name)
    $script:SuiteName = $Name
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
}

function Write-Result {
    param(
        [Parameter(Mandatory)][string]$TestId,
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][ValidateSet("PASS","FAIL","SKIP")][string]$Status,
        [string]$Expected = "",
        [string]$Actual = "",
        [string]$Note = ""
    )
    $entry = [PSCustomObject]@{
        Suite       = $script:SuiteName
        TestId      = $TestId
        Description = $Description
        Status      = $Status
        Expected    = $Expected
        Actual      = $Actual
        Note        = $Note
    }
    $script:TestResults.Add($entry) | Out-Null
    switch ($Status) {
        "PASS" {
            $script:PassCount++
            Write-Host "  [PASS] $TestId : $Description" -ForegroundColor Green
            if ($Note) { Write-Host "         note: $Note" -ForegroundColor DarkGray }
        }
        "FAIL" {
            $script:FailCount++
            Write-Host "  [FAIL] $TestId : $Description" -ForegroundColor Red
            if ($Expected) { Write-Host "         expected: $Expected" -ForegroundColor Yellow }
            if ($Actual)   { Write-Host "         actual:   $Actual" -ForegroundColor Yellow }
            if ($Note)     { Write-Host "         note:     $Note" -ForegroundColor Cyan }
        }
        "SKIP" {
            $script:SkipCount++
            Write-Host "  [SKIP] $TestId : $Description" -ForegroundColor Gray
            if ($Note) { Write-Host "         reason: $Note" -ForegroundColor Gray }
        }
    }
}

# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------
function Get-Summary {
    $total = $script:TestResults.Count
    Write-Host ""
    Write-Host "=== Summary ($script:SuiteName) ===" -ForegroundColor White
    Write-Host "Total: $total | PASS: $script:PassCount | FAIL: $script:FailCount | SKIP: $script:SkipCount" -ForegroundColor White
    return @{
        Total   = $total
        Pass    = $script:PassCount
        Fail    = $script:FailCount
        Skip    = $script:SkipCount
        Results = $script:TestResults
    }
}

function Reset-Results {
    $script:TestResults.Clear()
    $script:PassCount = 0
    $script:FailCount = 0
    $script:SkipCount = 0
}

# ---------------------------------------------------------------------------
# 等待服务就绪
# ---------------------------------------------------------------------------
function Wait-ServiceReady {
    param([int]$MaxRetries = 10, [int]$IntervalSec = 2)
    for ($i = 0; $i -lt $MaxRetries; $i++) {
        $r = Invoke-Api -Path "/healthz" -Method "GET" -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            return $true
        }
        Start-Sleep -Seconds $IntervalSec
    }
    return $false
}
