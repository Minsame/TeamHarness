# test_errors.ps1 - 错误处理测试
# 覆盖：鉴权错误、参数校验、边界值、SQL 注入、XSS

. "$PSScriptRoot\common.ps1"

Set-SuiteName "3. 错误处理"

if (-not (Wait-ServiceReady)) {
    Write-Host "FATAL: 服务未就绪" -ForegroundColor Red
    exit 1
}

$aliceKey = Issue-ApiKey -MemberId "alice" -AgentId "agent-alice"
if (-not $aliceKey) {
    Write-Host "FATAL: API Key 颁发失败" -ForegroundColor Red
    exit 1
}
$aliceHeaders = @{ "X-API-Key" = $aliceKey }

# ---------------------------------------------------------------------------
# E1: 无效 API Key（th_invalid）→ 401
# ---------------------------------------------------------------------------
function Test-E1 {
    $h = @{ "X-API-Key" = "th_invalid_key_12345" }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001" -Method "GET" -Headers $h
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "E1" -Description "无效 API Key 应返回 401" -Status "PASS" -Expected "401" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "E1" -Description "无效 API Key 应返回 401" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode)（无效 key 成功访问资产）" `
            -Note "/v1/assets/* 端点无鉴权中间件，不校验 X-API-Key"
    }
}
Test-E1

# ---------------------------------------------------------------------------
# E2: 缺失 X-API-Key 头 → 401
# ---------------------------------------------------------------------------
function Test-E2 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001" -Method "GET"
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "E2" -Description "缺失 X-API-Key 应返回 401" -Status "PASS" -Expected "401" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "E2" -Description "缺失 X-API-Key 应返回 401" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode)（无 key 也能访问）" `
            -Note "端点无鉴权依赖，不要求 X-API-Key 头"
    }
}
Test-E2

# ---------------------------------------------------------------------------
# E3: 空字符串 API Key → 401
# ---------------------------------------------------------------------------
function Test-E3 {
    $h = @{ "X-API-Key" = "" }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001" -Method "GET" -Headers $h
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "E3" -Description "空字符串 API Key 应返回 401" -Status "PASS" -Expected "401" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "E3" -Description "空字符串 API Key 应返回 401" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode)（空 key 也能访问）" `
            -Note "空字符串不被拒绝"
    }
}
Test-E3

# ---------------------------------------------------------------------------
# E4: 超长 API Key（10000 字符）→ 401 不应崩溃
# ---------------------------------------------------------------------------
function Test-E4 {
    $longKey = "th_" + ("a" * 10000)
    $h = @{ "X-API-Key" = $longKey }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001" -Method "GET" -Headers $h -TimeoutSec 20
    # 关键要求：不崩溃（非 500）
    if ($r.StatusCode -ge 500) {
        Write-Result -TestId "E4" -Description "超长 API Key 不应导致服务崩溃" -Status "FAIL" `
            -Expected "非 5xx（不崩溃）" -Actual "status=$($r.StatusCode)（服务端错误）"
    } else {
        $pass = $true
        $note = ""
        if ($r.StatusCode -eq 401) {
            $note = "正确返回 401"
        } elseif ($r.StatusCode -eq 400 -or $r.StatusCode -eq 431) {
            $note = "返回 $($r.StatusCode)（HTTP 层 header 长度限制，非鉴权拒绝，但未崩溃——可接受）"
        } elseif ($r.StatusCode -eq 200) {
            $note = "返回 200（无鉴权，超长 key 被忽略）"
        }
        Write-Result -TestId "E4" -Description "超长 API Key 不应导致服务崩溃" -Status "PASS" `
            -Expected "不崩溃（非 5xx）" -Actual "status=$($r.StatusCode)" -Note $note
    }
}
Test-E4

# ---------------------------------------------------------------------------
# E5: 创建关联缺失 dst_asset_id → 400（Pydantic 422）
# ---------------------------------------------------------------------------
function Test-E5 {
    $body = @{ link_type = "related_to" }  # 缺失 dst_asset_id
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body $body -Headers $aliceHeaders
    if ($r.StatusCode -eq 422 -or $r.StatusCode -eq 400) {
        Write-Result -TestId "E5" -Description "创建关联缺失 dst_asset_id 应被拒绝" -Status "PASS" `
            -Expected "400 或 422" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "E5" -Description "创建关联缺失 dst_asset_id 应被拒绝" -Status "FAIL" `
            -Expected "400 或 422" -Actual "status=$($r.StatusCode)"
    }
}
Test-E5

# ---------------------------------------------------------------------------
# E6: 创建关联无效 link_type → 400
# ---------------------------------------------------------------------------
function Test-E6 {
    $body = @{ dst_asset_id = "asset-alice-002"; link_type = "invalid_type" }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body $body -Headers $aliceHeaders
    if ($r.StatusCode -eq 400) {
        Write-Result -TestId "E6" -Description "创建关联无效 link_type 应返回 400" -Status "PASS" `
            -Expected "400" -Actual "status=$($r.StatusCode), detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "E6" -Description "创建关联无效 link_type 应返回 400" -Status "FAIL" `
            -Expected "400" -Actual "status=$($r.StatusCode)"
    }
}
Test-E6

# ---------------------------------------------------------------------------
# E7: 创建 ACL 无效 grantee_type → 400
# ---------------------------------------------------------------------------
function Test-E7 {
    $body = @{ grantee_type = "invalid"; grantee_id = "bob"; permission = "read" }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/acl" -Method "POST" -Body $body -Headers $aliceHeaders
    if ($r.StatusCode -eq 400) {
        Write-Result -TestId "E7" -Description "创建 ACL 无效 grantee_type 应返回 400" -Status "PASS" `
            -Expected "400" -Actual "status=$($r.StatusCode), detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "E7" -Description "创建 ACL 无效 grantee_type 应返回 400" -Status "FAIL" `
            -Expected "400" -Actual "status=$($r.StatusCode)"
    }
}
Test-E7

# ---------------------------------------------------------------------------
# E8: 创建 ACL 无效 permission → 400
# ---------------------------------------------------------------------------
function Test-E8 {
    $body = @{ grantee_type = "user"; grantee_id = "bob"; permission = "invalid" }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/acl" -Method "POST" -Body $body -Headers $aliceHeaders
    if ($r.StatusCode -eq 400) {
        Write-Result -TestId "E8" -Description "创建 ACL 无效 permission 应返回 400" -Status "PASS" `
            -Expected "400" -Actual "status=$($r.StatusCode), detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "E8" -Description "创建 ACL 无效 permission 应返回 400" -Status "FAIL" `
            -Expected "400" -Actual "status=$($r.StatusCode)"
    }
}
Test-E8

# ---------------------------------------------------------------------------
# E9: BFS 遍历 depth=0 → 422（Query ge=1）
# ---------------------------------------------------------------------------
function Test-E9 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=0" -Method "GET" -Headers $aliceHeaders
    if ($r.StatusCode -eq 422) {
        Write-Result -TestId "E9" -Description "BFS depth=0 应返回 422（ge=1）" -Status "PASS" `
            -Expected "422" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "E9" -Description "BFS depth=0 应返回 422（ge=1）" -Status "FAIL" `
            -Expected "422" -Actual "status=$($r.StatusCode)"
    }
}
Test-E9

# ---------------------------------------------------------------------------
# E10: BFS 遍历 depth=4 → 422（Query le=3）
# ---------------------------------------------------------------------------
function Test-E10 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=4" -Method "GET" -Headers $aliceHeaders
    if ($r.StatusCode -eq 422) {
        Write-Result -TestId "E10" -Description "BFS depth=4 应返回 422（le=3）" -Status "PASS" `
            -Expected "422" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "E10" -Description "BFS depth=4 应返回 422（le=3）" -Status "FAIL" `
            -Expected "422" -Actual "status=$($r.StatusCode)"
    }
}
Test-E10

# ---------------------------------------------------------------------------
# E11: 不存在的资产 ID → 404
# ---------------------------------------------------------------------------
function Test-E11 {
    $r = Invoke-Api -Path "/v1/assets/asset-nonexistent-99999" -Method "GET" -Headers $aliceHeaders
    if ($r.StatusCode -eq 404) {
        Write-Result -TestId "E11" -Description "不存在的资产 ID 应返回 404" -Status "PASS" `
            -Expected "404" -Actual "status=$($r.StatusCode), detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "E11" -Description "不存在的资产 ID 应返回 404" -Status "FAIL" `
            -Expected "404" -Actual "status=$($r.StatusCode)"
    }
}
Test-E11

# ---------------------------------------------------------------------------
# E12: SQL 注入尝试：asset_id = "'; DROP TABLE; --" → 不应崩溃
# ---------------------------------------------------------------------------
function Test-E12 {
    # URL 编码特殊字符
    $injection = "'; DROP TABLE asset_index; --"
    $encodedId = [uri]::EscapeDataString($injection)
    $r = Invoke-Api -Path "/v1/assets/$encodedId" -Method "GET" -Headers $aliceHeaders -TimeoutSec 10
    if ($r.StatusCode -ge 500) {
        Write-Result -TestId "E12" -Description "SQL 注入不应导致崩溃" -Status "FAIL" `
            -Expected "非 5xx（不崩溃）" -Actual "status=$($r.StatusCode), error=$($r.Error)"
    } else {
        # 验证 asset_index 表仍然存在（查询列表确认）
        $check = Invoke-Api -Path "/v1/assets?limit=1" -Method "GET" -Headers $aliceHeaders
        $tableIntact = $check.StatusCode -eq 200
        if ($tableIntact) {
            Write-Result -TestId "E12" -Description "SQL 注入不应导致崩溃或数据损坏" -Status "PASS" `
                -Expected "非 5xx，表未损坏" -Actual "status=$($r.StatusCode)，注入后表仍可查询" `
                -Note "SQLAlchemy 参数化查询防止了注入"
        } else {
            Write-Result -TestId "E12" -Description "SQL 注入不应导致崩溃或数据损坏" -Status "FAIL" `
                -Expected "表未损坏" -Actual "注入后表查询失败: status=$($check.StatusCode)"
        }
    }
}
Test-E12

# ---------------------------------------------------------------------------
# E13: XSS 尝试：category = "<script>alert(1)</script>" → 应被转义或拒绝
# ---------------------------------------------------------------------------
function Test-E13 {
    $xss = "<script>alert(1)</script>"
    $encodedCategory = [uri]::EscapeDataString($xss)
    $r = Invoke-Api -Path "/v1/assets?category=$encodedCategory&limit=10" -Method "GET" -Headers $aliceHeaders
    if ($r.StatusCode -ge 500) {
        Write-Result -TestId "E13" -Description "XSS 注入不应导致崩溃" -Status "FAIL" `
            -Expected "非 5xx" -Actual "status=$($r.StatusCode)"
        return
    }
    # 检查响应中是否包含原始 script 标签（未转义）
    $bodyRaw = $r.Body
    $containsScript = $bodyRaw -match "<script>alert\(1\)</script>"
    if (-not $containsScript) {
        Write-Result -TestId "E13" -Description "XSS 注入应被转义或拒绝" -Status "PASS" `
            -Expected "响应中不包含原始 script 标签" -Actual "status=$($r.StatusCode)，响应中未发现未转义的 <script>" `
            -Note "JSON API 天然不执行 HTML，且 SQLAlchemy 参数化查询阻止注入"
    } else {
        Write-Result -TestId "E13" -Description "XSS 注入应被转义或拒绝" -Status "FAIL" `
            -Expected "响应中不包含原始 script 标签" -Actual "响应中发现未转义的 <script> 标签"
    }
}
Test-E13

Get-Summary | Out-Null
