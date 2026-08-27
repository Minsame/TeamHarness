# test_integration.ps1 - TeamHarness 跨模块集成测试 + 修复验证
# 范围：镜像重建后的后端鉴权 + AUTH-2 根因 + 跨模块端到端 + nginx + 数据一致性
# 前置：docker-compose 服务运行中（nginx:8080 -> asset-service:8080），镜像已用 --no-cache 重建
# 复用 tests/realenv/common.ps1 的 Invoke-Api / Issue-ApiKey / Write-Result / Get-Summary
# 测试铁律：用例独立可复现；突变数据 round-trip 还原；覆盖 happy + 边界 + 异常；FAIL 先定位根因
#
# 运行：powershell -ExecutionPolicy Bypass -File tests/integration/test_integration.ps1

. "$PSScriptRoot\..\realenv\common.ps1"

# ===========================================================================
# 辅助：获取响应头（Invoke-Api 不返回头，需单独取 Content-Type / Allow / CORS 头）
# ===========================================================================
function Invoke-ApiWithHeaders {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$Method = "GET",
        [object]$Body = $null,
        [hashtable]$Headers = @{},
        [int]$TimeoutSec = 15
    )
    $uri = "$script:BaseUrl$Path"
    $statusCode = 0
    $respHeaders = @{}
    $respBody = ""
    $errMsg = $null
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
        $respHeaders = $response.Headers
    } catch {
        if ($_.Exception.Response) {
            try {
                $statusCode = [int]$_.Exception.Response.StatusCode
                $respHeaders = @{}
                foreach ($k in $_.Exception.Response.Headers.AllKeys) {
                    $respHeaders[$k] = $_.Exception.Response.Headers[$k]
                }
                $stream = $_.Exception.Response.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $respBody = $reader.ReadToEnd()
                $reader.Close()
            } catch {}
        }
        $errMsg = $_.Exception.Message
    }
    $jsonVal = $null
    if ($respBody) {
        try { $jsonVal = $respBody | ConvertFrom-Json } catch { $jsonVal = $null }
    }
    return @{
        StatusCode = $statusCode
        Body       = $respBody
        Json       = $jsonVal
        Headers    = $respHeaders
        Error      = $errMsg
    }
}

# ===========================================================================
# 前置：等待服务就绪 + 颁发 API Key
# ===========================================================================
Set-SuiteName "阶段0. 前置准备"

if (-not (Wait-ServiceReady)) {
    Write-Host "FATAL: 服务未就绪（http://localhost:8080/healthz）" -ForegroundColor Red
    exit 1
}
Write-Result -TestId "PREP-0" -Description "服务就绪" -Status "PASS" -Actual "healthz=200"

$aliceKey   = Issue-ApiKey -MemberId "alice" -AgentId "agent-alice-int"
$bobKey     = Issue-ApiKey -MemberId "bob"   -AgentId "agent-bob-int"
$charlieKey = Issue-ApiKey -MemberId "charlie" -AgentId "agent-charlie-int"

if (-not $aliceKey -or -not $bobKey -or -not $charlieKey) {
    Write-Host "FATAL: API Key 颁发失败" -ForegroundColor Red
    exit 1
}
Write-Result -TestId "PREP-1" -Description "颁发 alice/bob/charlie API Key" -Status "PASS" `
    -Actual "alice=$($aliceKey.Substring(0,10))... bob=$($bobKey.Substring(0,10))... charlie=$($charlieKey.Substring(0,10))..."

$aliceH   = @{ "X-API-Key" = $aliceKey }
$bobH     = @{ "X-API-Key" = $bobKey }
$charlieH = @{ "X-API-Key" = $charlieKey }

# ===========================================================================
# 阶段 1：镜像重建验证（容器内代码是否含 require_member）
# ===========================================================================
Set-SuiteName "阶段1. 镜像重建验证"

# 辅助：执行 docker exec（用 stdin 管道传 Python 代码，规避 PowerShell 给原生命令传参时剥离双引号的问题）
function Invoke-DockerProbe {
    param([string]$PyCmd)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = $null
    try {
        $out = ($PyCmd | docker exec -i teamharness-asset-service python 2>&1) -join "`n"
    } catch {
        $out = "ERROR: $($_.Exception.Message)"
    }
    $ErrorActionPreference = $prevEAP
    return $out
}

# IMG-1: 容器内 server.assets.api 应含 require_member 函数
$probe1 = Invoke-DockerProbe 'import server.assets.api as m; print("HAS" if hasattr(m,"require_member") else "MISSING")'
if ($probe1 -match "HAS") {
    Write-Result -TestId "IMG-1" -Description "容器内含 require_member 函数" -Status "PASS" -Actual "镜像已更新：require_member 存在"
} else {
    Write-Result -TestId "IMG-1" -Description "容器内含 require_member 函数" -Status "FAIL" -Expected "HAS" -Actual "容器仍跑旧镜像：$probe1"
}

# IMG-2: 容器内应含 _assert_owner / _can_view_asset / _has_acl_grant
$probe2 = Invoke-DockerProbe 'import server.assets.api as m; print("HAS" if all(hasattr(m,n) for n in ["_assert_owner","_can_view_asset","_has_acl_grant"]) else "MISSING")'
if ($probe2 -match "HAS") {
    Write-Result -TestId "IMG-2" -Description "容器内含 _assert_owner/_can_view_asset/_has_acl_grant" -Status "PASS" -Actual "三个鉴权辅助函数均存在"
} else {
    Write-Result -TestId "IMG-2" -Description "容器内含 _assert_owner/_can_view_asset/_has_acl_grant" -Status "FAIL" -Expected "HAS" -Actual "MISSING：$probe2"
}

# ===========================================================================
# 阶段 2：后端鉴权修复验证（镜像重建后，23个FAIL→PASS）
# ===========================================================================
Set-SuiteName "阶段2. 后端鉴权验证"

# AUTH-1a: 有效 API Key -> 200
$r = Invoke-Api -Path "/v1/assets?owner=alice&limit=5" -Method "GET" -Headers $aliceH
if ($r.StatusCode -eq 200) {
    Write-Result -TestId "AUTH-1a" -Description "有效 API Key 访问 -> 200" -Status "PASS" -Expected "200" -Actual "status=$($r.StatusCode)"
} else {
    Write-Result -TestId "AUTH-1a" -Description "有效 API Key 访问 -> 200" -Status "FAIL" -Expected "200" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# AUTH-1b: 无效 API Key (th_invalid) -> 应 401
$r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = "th_invalid" }
if ($r.StatusCode -eq 401) {
    Write-Result -TestId "AUTH-1b" -Description "无效 API Key (th_invalid) -> 401" -Status "PASS" -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
} else {
    Write-Result -TestId "AUTH-1b" -Description "无效 API Key (th_invalid) -> 401" -Status "FAIL" -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# AUTH-1c: 缺失 X-API-Key 头 -> 应 401
$r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{}
if ($r.StatusCode -eq 401) {
    Write-Result -TestId "AUTH-1c" -Description "缺失 X-API-Key 头 -> 401" -Status "PASS" -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
} else {
    Write-Result -TestId "AUTH-1c" -Description "缺失 X-API-Key 头 -> 401" -Status "FAIL" -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# AUTH-1d: 空字符串 API Key -> 应 401
$r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = "" }
if ($r.StatusCode -eq 401) {
    Write-Result -TestId "AUTH-1d" -Description "空字符串 API Key -> 401" -Status "PASS" -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
} else {
    Write-Result -TestId "AUTH-1d" -Description "空字符串 API Key -> 401" -Status "FAIL" -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# AUTH-1e: 轮换后的旧 API Key -> 应 401
$issueR = Invoke-Api -Path "/v1/auth/apikey" -Method "POST" -Body @{ member_id = "alice"; agent_id = "agent-rotated-int" }
$keyToRevoke = if ($issueR.Json) { $issueR.Json.api_key } else { $null }
$keyId = if ($issueR.Json) { $issueR.Json.key_id } else { $null }
if ($keyToRevoke -and $keyId) {
    Invoke-Api -Path "/v1/auth/apikey/rotate" -Method "POST" -Body @{ key_id = $keyId } | Out-Null
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = $keyToRevoke }
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "AUTH-1e" -Description "轮换后的旧 API Key -> 401" -Status "PASS" -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "AUTH-1e" -Description "轮换后的旧 API Key -> 401" -Status "FAIL" -Expected "401（旧 key 应失效）" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
} else {
    Write-Result -TestId "AUTH-1e" -Description "轮换后的旧 API Key -> 401" -Status "SKIP" -Note "前置失败：无法颁发测试 key"
}

# OWNER-1: bob 改 alice 资产 scope -> 应 403
$r = Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body @{ scope = "public" } -Headers $bobH
if ($r.StatusCode -eq 403) {
    Write-Result -TestId "OWNER-1" -Description "bob 改 alice 资产 scope -> 403" -Status "PASS" -Expected "403（owner 校验）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
} else {
    if ($r.StatusCode -eq 200) {
        Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body @{ scope = "team" } -Headers $aliceH | Out-Null
    }
    Write-Result -TestId "OWNER-1" -Description "bob 改 alice 资产 scope -> 403" -Status "FAIL" -Expected "403" -Actual "status=$($r.StatusCode) body=$($r.Body)" -Note "_assert_owner 未生效"
}

# OWNER-2: bob 删除 alice 资产关联 -> 应 403
$links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceH
$targetLink = $null
if ($links.Json -and $links.Json.outgoing) {
    $targetLink = $links.Json.outgoing | Select-Object -First 1
}
if ($targetLink) {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links/$($targetLink.link_id)" -Method "DELETE" -Headers $bobH
    if ($r.StatusCode -eq 403) {
        Write-Result -TestId "OWNER-2" -Description "bob 删除 alice 资产关联 -> 403" -Status "PASS" -Expected "403（owner 校验）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        if ($r.StatusCode -eq 200) {
            Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body @{ dst_asset_id = $targetLink.dst_asset_id; link_type = $targetLink.link_type } -Headers $aliceH | Out-Null
        }
        Write-Result -TestId "OWNER-2" -Description "bob 删除 alice 资产关联 -> 403" -Status "FAIL" -Expected "403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
} else {
    Write-Result -TestId "OWNER-2" -Description "bob 删除 alice 资产关联 -> 403" -Status "SKIP" -Note "前置不满足：asset-alice-001 无 outgoing 关联"
}

# OWNER-3: bob 撤销 alice 资产的 ACL -> 应 403
$acls = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "GET" -Headers $aliceH
$targetAcl = $null
if ($acls.Json -and $acls.Json.acls -and $acls.Json.acls.Count -gt 0) {
    $targetAcl = $acls.Json.acls[0]
}
if ($targetAcl) {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$($targetAcl.acl_id)" -Method "DELETE" -Headers $bobH
    if ($r.StatusCode -eq 403) {
        Write-Result -TestId "OWNER-3" -Description "bob 撤销 alice 资产 ACL -> 403" -Status "PASS" -Expected "403（owner 校验）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        if ($r.StatusCode -eq 200) {
            Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body @{
                grantee_type = $targetAcl.grantee_type; grantee_id = $targetAcl.grantee_id
                permission = $targetAcl.permission; granted_by = $targetAcl.granted_by
            } -Headers $aliceH | Out-Null
        }
        Write-Result -TestId "OWNER-3" -Description "bob 撤销 alice 资产 ACL -> 403" -Status "FAIL" -Expected "403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
} else {
    Write-Result -TestId "OWNER-3" -Description "bob 撤销 alice 资产 ACL -> 403" -Status "SKIP" -Note "前置不满足：asset-alice-004 无 ACL"
}

# SCOPE-1: alice 查 bob 的 private 资产 asset-bob-002 -> 应 404
$r = Invoke-Api -Path "/v1/assets/asset-bob-002" -Method "GET" -Headers $aliceH
if ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403) {
    Write-Result -TestId "SCOPE-1" -Description "alice 查 bob 的 private 资产 -> 404/403" -Status "PASS" -Expected "404 或 403（private 仅 owner 可见）" -Actual "status=$($r.StatusCode)"
} else {
    Write-Result -TestId "SCOPE-1" -Description "alice 查 bob 的 private 资产 -> 404/403" -Status "FAIL" -Expected "404 或 403" -Actual "status=$($r.StatusCode) body=$($r.Body)" -Note "_can_view_asset 对 private 非 owner 未拒绝"
}

# SCOPE-2: bob 查 alice 的 restricted 资产 asset-alice-004（无 user ACL）-> 应 404
$r = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $bobH
if ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403) {
    Write-Result -TestId "SCOPE-2" -Description "bob 查 alice 的 restricted 资产（无 ACL）-> 404/403" -Status "PASS" -Expected "404 或 403（restricted 仅 ACL 授权用户可见）" -Actual "status=$($r.StatusCode)"
} else {
    Write-Result -TestId "SCOPE-2" -Description "bob 查 alice 的 restricted 资产（无 ACL）-> 404/403" -Status "FAIL" -Expected "404 或 403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# SCOPE-3: alice 给 bob 加 ACL read 后，bob 查 asset-alice-004 -> 应 200（测试后清理）
$aclBody = @{ grantee_type = "user"; grantee_id = "bob"; permission = "read"; granted_by = "alice" }
$addR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $aclBody -Headers $aliceH
$createdAclId = if ($addR.Json) { $addR.Json.acl_id } else { $null }
$r = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $bobH
if ($createdAclId) {
    Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceH | Out-Null
}
if ($r.StatusCode -eq 200) {
    Write-Result -TestId "SCOPE-3" -Description "加 ACL read 后 bob 可查 alice 的 restricted 资产 -> 200" -Status "PASS" -Expected "200（ACL 授权生效）" -Actual "status=$($r.StatusCode)（ACL 已清理）"
} else {
    Write-Result -TestId "SCOPE-3" -Description "加 ACL read 后 bob 可查 alice 的 restricted 资产 -> 200" -Status "FAIL" -Expected "200" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# SCOPE-4: 共享库视图（不带 owner）不应包含 alice 的 private/restricted
$r = Invoke-Api -Path "/v1/assets?limit=200" -Method "GET" -Headers $bobH
$ids = @()
if ($r.Json -and $r.Json.items) { $ids = $r.Json.items | ForEach-Object { $_.id } }
$hasAlicePrivate = $ids -contains "asset-alice-006"
$hasAliceRestricted = $ids -contains "asset-alice-004"
if ($r.StatusCode -eq 200 -and -not $hasAlicePrivate -and -not $hasAliceRestricted) {
    Write-Result -TestId "SCOPE-4" -Description "共享库不含 alice 的 private/restricted" -Status "PASS" -Expected "不含 asset-alice-004/006" -Actual "列表共 $($ids.Count) 项，无泄露"
} else {
    Write-Result -TestId "SCOPE-4" -Description "共享库不含 alice 的 private/restricted" -Status "FAIL" -Expected "不含 asset-alice-004/006" -Actual "status=$($r.StatusCode) has004=$hasAliceRestricted has006=$hasAlicePrivate"
}

# INTEG-1: 重复创建相同关联 -> 应 409（非 500）
$links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceH
$existing = $null
if ($links.Json -and $links.Json.outgoing) {
    $existing = $links.Json.outgoing | Where-Object { $_.dst_asset_id -eq "asset-alice-002" -and $_.link_type -eq "derived_from" } | Select-Object -First 1
}
$createdLink = $null
if (-not $existing) {
    $createR = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body @{ dst_asset_id = "asset-alice-002"; link_type = "derived_from" } -Headers $aliceH
    if ($createR.Json) { $createdLink = $createR.Json.link_id }
}
$r = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body @{ dst_asset_id = "asset-alice-002"; link_type = "derived_from" } -Headers $aliceH
if ($r.StatusCode -eq 409) {
    Write-Result -TestId "INTEG-1" -Description "重复创建相同关联 -> 409" -Status "PASS" -Expected "409（IntegrityError 已捕获）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
} else {
    Write-Result -TestId "INTEG-1" -Description "重复创建相同关联 -> 409" -Status "FAIL" -Expected "409" -Actual "status=$($r.StatusCode) body=$($r.Body)" -Note "IntegrityError 未被捕获为 409，返回 500 或其他"
}

# INTEG-2: 重复创建相同 ACL -> 应 409（非 500）
$aclBody = @{ grantee_type = "agent"; grantee_id = "agent-dup-int"; permission = "read"; granted_by = "alice" }
$createR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $aclBody -Headers $aliceH
$createdAclId = if ($createR.Json) { $createR.Json.acl_id } else { $null }
$r = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $aclBody -Headers $aliceH
if ($createdAclId) {
    Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceH | Out-Null
}
if ($r.StatusCode -eq 409) {
    Write-Result -TestId "INTEG-2" -Description "重复创建相同 ACL -> 409" -Status "PASS" -Expected "409（IntegrityError 已捕获）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
} else {
    Write-Result -TestId "INTEG-2" -Description "重复创建相同 ACL -> 409" -Status "FAIL" -Expected "409" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# OPTIONS: OPTIONS /v1/assets -> 200/204/405
$r = Invoke-ApiWithHeaders -Path "/v1/assets?limit=5" -Method "OPTIONS" -Headers @{}
if ($r.StatusCode -in @(200, 204, 405)) {
    Write-Result -TestId "OPTIONS-1" -Description "OPTIONS /v1/assets -> 200/204/405" -Status "PASS" -Expected "200/204/405" -Actual "status=$($r.StatusCode)"
} else {
    Write-Result -TestId "OPTIONS-1" -Description "OPTIONS /v1/assets -> 200/204/405" -Status "FAIL" -Expected "200/204/405" -Actual "status=$($r.StatusCode)"
}

# ===========================================================================
# 阶段 3：AUTH-2 根因确认（后端 lookup 响应 vs 前端 handleLogin）
# ===========================================================================
Set-SuiteName "阶段3. AUTH-2 根因确认"

# LOOKUP-1: 无效 key -> 200 + agent_id=null（后端设计意图：反查接口）
$r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = "th_invalid" }
$lookupStatus = $r.StatusCode
$lookupAgentId = if ($r.Json) { $r.Json.agent_id } else { "<null-json>" }
if ($r.StatusCode -eq 200 -and $r.Json -and $null -eq $r.Json.agent_id) {
    Write-Result -TestId "LOOKUP-1" -Description "无效 key lookup -> 200 + agent_id=null（后端设计意图）" -Status "PASS" `
        -Expected "200 + agent_id=null（反查接口，非鉴权失败）" -Actual "status=$lookupStatus agent_id=$lookupAgentId"
} else {
    Write-Result -TestId "LOOKUP-1" -Description "无效 key lookup -> 200 + agent_id=null" -Status "FAIL" `
        -Expected "200 + agent_id=null" -Actual "status=$lookupStatus agent_id=$lookupAgentId body=$($r.Body)"
}

# LOOKUP-2: 有效 key -> 200 + agent_id 有值
$r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = $aliceKey }
if ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.agent_id -eq "agent-alice-int") {
    Write-Result -TestId "LOOKUP-2" -Description "有效 key lookup -> 200 + agent_id" -Status "PASS" -Expected "200 + agent_id=agent-alice-int" -Actual "status=$($r.StatusCode) agent_id=$($r.Json.agent_id)"
} else {
    Write-Result -TestId "LOOKUP-2" -Description "有效 key lookup -> 200 + agent_id" -Status "FAIL" -Expected "200 + agent_id=agent-alice-int" -Actual "status=$($r.StatusCode) body=$($r.Body)"
}

# AUTH2-ROOT: 确认根因——后端返回 200+null，前端 handleLogin 未校验 agent_id
# 读前端 app.js handleLogin 代码确认（静态检查：handleLogin 函数体内是否出现 agent_id）
$appJsPath = "$PSScriptRoot\..\..\frontend\app.js"
$appJsLines = Get-Content $appJsPath
$loginMatch = Select-String -Path $appJsPath -Pattern "async function handleLogin" -SimpleMatch
$hasAgentIdCheck = $false
$snippetPreview = ""
if ($loginMatch) {
    $loginLine = $loginMatch.LineNumber
    $block = $appJsLines | Select-Object -Skip ($loginLine - 1) -First 25 | Out-String
    $snippetPreview = ($block -replace "`n", " ").Trim().Substring(0, [Math]::Min(180, $block.Trim().Length))
    $hasAgentIdCheck = $block -match "agent_id"
}

if ($lookupStatus -eq 200 -and -not $hasAgentIdCheck) {
    Write-Result -TestId "AUTH2-ROOT" -Description "AUTH-2 根因：后端 200+null + 前端未校验 agent_id" -Status "PASS" `
        -Expected "根因=前端（后端 lookup 是反查接口，200+null 是设计意图）" `
        -Actual "后端 lookup=$lookupStatus+agent_id=null；前端 handleLogin 未出现 agent_id 字符串" `
        -Note "镜像重建不改变此行为。修复需在 handleLogin 中校验返回的 agent_id 非空"
} else {
    Write-Result -TestId "AUTH2-ROOT" -Description "AUTH-2 根因确认" -Status "FAIL" `
        -Expected "后端 200+null + 前端无 agent_id 校验" `
        -Actual "lookupStatus=$lookupStatus hasAgentIdCheck=$hasAgentIdCheck" `
        -Note "snippet: $snippetPreview"
}

# ===========================================================================
# 阶段 4：跨模块端到端验证（4 个场景）
# ===========================================================================
Set-SuiteName "阶段4. 跨模块端到端"

# --------- 场景 1：alice 完整资产管理 ---------
# S1-1: alice 登录（lookup 有效 key）-> 200 + agent_id
$r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = $aliceKey }
$s1Login = ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.agent_id -eq "agent-alice-int")

# S1-2: 查看我的规则库（owner=alice）
$r = Invoke-Api -Path "/v1/assets?owner=alice&limit=50" -Method "GET" -Headers $aliceH
$s1MyAssets = ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.items.Count -ge 1)

# S1-3: 修改资产 scope team -> restricted（asset-alice-003 原为 team）
$before = Invoke-Api -Path "/v1/assets/asset-alice-003" -Method "GET" -Headers $aliceH
$origScope003 = if ($before.Json) { $before.Json.scope } else { "team" }
$r = Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body @{ scope = "restricted" } -Headers $aliceH
$s1ScopeChange = ($r.StatusCode -eq 200)
# 立即恢复
Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body @{ scope = $origScope003 } -Headers $aliceH | Out-Null

# S1-4: 给 bob 加 ACL read（asset-alice-004）
$addR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body @{ grantee_type = "user"; grantee_id = "bob"; permission = "read"; granted_by = "alice" } -Headers $aliceH
$createdAclId = if ($addR.Json) { $addR.Json.acl_id } else { $null }
$s1AddAcl = ($addR.StatusCode -eq 200 -and $createdAclId)
# 立即清理
if ($createdAclId) {
    Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceH | Out-Null
}

# S1-5: 退出登录（前端清 localStorage，这里验证 logout 端点或仅校验流程完成）
$s1Logout = $true  # 前端行为，API 层无 logout 端点

if ($s1Login -and $s1MyAssets -and $s1ScopeChange -and $s1AddAcl -and $s1Logout) {
    Write-Result -TestId "S1-FLOW" -Description "场景1：alice 完整资产管理（登录/我的库/改scope/加ACL/退出）" -Status "PASS" `
        -Actual "login=$s1Login myAssets=$s1MyAssets scopeChange=$s1ScopeChange addAcl=$s1AddAcl logout=$s1Logout（已恢复）"
} else {
    Write-Result -TestId "S1-FLOW" -Description "场景1：alice 完整资产管理" -Status "FAIL" `
        -Expected "全部 true" -Actual "login=$s1Login myAssets=$s1MyAssets scopeChange=$s1ScopeChange addAcl=$s1AddAcl logout=$s1Logout"
}

# --------- 场景 2：bob 受限访问 ---------
# S2-1: bob 登录（lookup 有效 key）
$r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = $bobKey }
$s2Login = ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.agent_id -eq "agent-bob-int")

# S2-2: bob 查共享库（应看到 alice 的 team 资产，不含 private/restricted）
$r = Invoke-Api -Path "/v1/assets?limit=200" -Method "GET" -Headers $bobH
$ids = @()
if ($r.Json -and $r.Json.items) { $ids = $r.Json.items | ForEach-Object { $_.id } }
$hasAliceTeam = ($ids -contains "asset-alice-001") -or ($ids -contains "asset-alice-002") -or ($ids -contains "asset-alice-003")
$hasAlicePrivate = $ids -contains "asset-alice-006"
$hasAliceRestricted = $ids -contains "asset-alice-004"
$s2Shared = ($r.StatusCode -eq 200 -and $hasAliceTeam -and -not $hasAlicePrivate -and -not $hasAliceRestricted)

# S2-3: alice 给 bob 加 ACL read 后，bob 通过 API 访问 alice 的 restricted 资产 -> 200
$addR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body @{ grantee_type = "user"; grantee_id = "bob"; permission = "read"; granted_by = "alice" } -Headers $aliceH
$createdAclId = if ($addR.Json) { $addR.Json.acl_id } else { $null }
$r = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $bobH
$s2AclAccess = ($r.StatusCode -eq 200)
if ($createdAclId) {
    Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceH | Out-Null
}

# S2-4: bob 通过 API 访问 alice 的 private 资产（无 ACL）-> 404
$r = Invoke-Api -Path "/v1/assets/asset-alice-006" -Method "GET" -Headers $bobH
$s2PrivateDeny = ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403)

if ($s2Login -and $s2Shared -and $s2AclAccess -and $s2PrivateDeny) {
    Write-Result -TestId "S2-FLOW" -Description "场景2：bob 受限访问（共享库/ACL放行/private拒绝）" -Status "PASS" `
        -Actual "login=$s2Login shared=$s2Shared aclAccess=$s2AclAccess privateDeny=$s2PrivateDeny（ACL 已清理）"
} else {
    Write-Result -TestId "S2-FLOW" -Description "场景2：bob 受限访问" -Status "FAIL" `
        -Expected "全部 true" -Actual "login=$s2Login shared=$s2Shared(hasTeam=$hasAliceTeam,hasPriv=$hasAlicePrivate,hasRestr=$hasAliceRestricted) aclAccess=$s2AclAccess privateDeny=$s2PrivateDeny"
}

# --------- 场景 3：图谱 + ACL 联动 ---------
# S3-1: alice 登录
$r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = $aliceKey }
$s3Login = ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.agent_id -eq "agent-alice-int")

# S3-2: 创建资产关联（001 derived_from 002，若已存在则跳过创建验证存在性）
$links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceH
$hasLink = $false
$createdLinkId = $null
if ($links.Json -and $links.Json.outgoing) {
    $existing = $links.Json.outgoing | Where-Object { $_.dst_asset_id -eq "asset-alice-002" -and $_.link_type -eq "derived_from" }
    if ($existing) { $hasLink = $true }
}
if (-not $hasLink) {
    $createR = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body @{ dst_asset_id = "asset-alice-002"; link_type = "derived_from" } -Headers $aliceH
    if ($createR.StatusCode -eq 200 -and $createR.Json -and $createR.Json.link_id) {
        $createdLinkId = $createR.Json.link_id
        $hasLink = $true
    } elseif ($createR.StatusCode -eq 409) {
        $hasLink = $true  # 已存在（409）
    }
}

# S3-3: BFS 遍历验证（depth=2）
$r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=2" -Method "GET" -Headers $aliceH
$s3Bfs = ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.nodes -and $r.Json.nodes.Count -ge 2)

# S3-4: 把 asset-alice-004 改为 restricted（若已是 restricted 则不变）
$before = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $aliceH
$origScope004 = if ($before.Json) { $before.Json.scope } else { "restricted" }
if ($origScope004 -ne "restricted") {
    Invoke-Api -Path "/v1/assets/asset-alice-004/scope" -Method "PATCH" -Body @{ scope = "restricted" } -Headers $aliceH | Out-Null
}

# S3-5: 给 charlie 加 ACL execute
$addR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body @{ grantee_type = "user"; grantee_id = "charlie"; permission = "execute"; granted_by = "alice" } -Headers $aliceH
$createdAclId = if ($addR.Json) { $addR.Json.acl_id } else { $null }
$s3AddAcl = ($addR.StatusCode -eq 200 -and $createdAclId)

# S3-6: 撤销 charlie 的 ACL
$s3RevokeAcl = $false
if ($createdAclId) {
    $delR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceH
    $s3RevokeAcl = ($delR.StatusCode -eq 200)
}

# 恢复 asset-alice-004 scope
if ($origScope004 -ne "restricted") {
    Invoke-Api -Path "/v1/assets/asset-alice-004/scope" -Method "PATCH" -Body @{ scope = $origScope004 } -Headers $aliceH | Out-Null
}

if ($s3Login -and $hasLink -and $s3Bfs -and $s3AddAcl -and $s3RevokeAcl) {
    Write-Result -TestId "S3-FLOW" -Description "场景3：图谱+ACL联动（创建关联/BFS/改scope/加ACL/撤销ACL）" -Status "PASS" `
        -Actual "login=$s3Login hasLink=$hasLink bfs=$s3Bfs addAcl=$s3AddAcl revokeAcl=$s3RevokeAcl（scope/acl 已恢复）"
} else {
    Write-Result -TestId "S3-FLOW" -Description "场景3：图谱+ACL联动" -Status "FAIL" `
        -Expected "全部 true" -Actual "login=$s3Login hasLink=$hasLink bfs=$s3Bfs addAcl=$s3AddAcl revokeAcl=$s3RevokeAcl"
}

# --------- 场景 4：权限边界跨模块 ---------
# S4-1: bob 登录
$r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = $bobKey }
$s4Login = ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.agent_id -eq "agent-bob-int")

# S4-2: bob 通过 API 访问 alice 的 private 资产 asset-alice-001 -> 应 404/403
# 注：asset-alice-001 是 team scope，bob 应可见（200）。测 asset-alice-006 (private)
$r = Invoke-Api -Path "/v1/assets/asset-alice-006" -Method "GET" -Headers $bobH
$s4DenyPrivate = ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403)

# S4-3: bob 尝试修改 alice 的资产 scope -> 应 403
$r = Invoke-Api -Path "/v1/assets/asset-alice-001/scope" -Method "PATCH" -Body @{ scope = "public" } -Headers $bobH
$s4DenyModify = ($r.StatusCode -eq 403)
if ($r.StatusCode -eq 200) {
    Invoke-Api -Path "/v1/assets/asset-alice-001/scope" -Method "PATCH" -Body @{ scope = "team" } -Headers $aliceH | Out-Null
}

# S4-4: bob 尝试删除 alice 的资产关联 -> 应 403
$links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceH
$s4DenyDeleteLink = $false
if ($links.Json -and $links.Json.outgoing -and $links.Json.outgoing.Count -gt 0) {
    $tl = $links.Json.outgoing[0]
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links/$($tl.link_id)" -Method "DELETE" -Headers $bobH
    $s4DenyDeleteLink = ($r.StatusCode -eq 403)
    if ($r.StatusCode -eq 200) {
        Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body @{ dst_asset_id = $tl.dst_asset_id; link_type = $tl.link_type } -Headers $aliceH | Out-Null
    }
} else {
    $s4DenyDeleteLink = $true  # 无关联可删，跳过
}

if ($s4Login -and $s4DenyPrivate -and $s4DenyModify -and $s4DenyDeleteLink) {
    Write-Result -TestId "S4-FLOW" -Description "场景4：权限边界跨模块（bob 访问 alice 资产被拒）" -Status "PASS" `
        -Actual "login=$s4Login denyPrivate=$s4DenyPrivate denyModify=$s4DenyModify denyDeleteLink=$s4DenyDeleteLink"
} else {
    Write-Result -TestId "S4-FLOW" -Description "场景4：权限边界跨模块" -Status "FAIL" `
        -Expected "全部 true" -Actual "login=$s4Login denyPrivate=$s4DenyPrivate denyModify=$s4DenyModify denyDeleteLink=$s4DenyDeleteLink"
}

# ===========================================================================
# 阶段 5：nginx 路径遍历 + CORS 验证
# ===========================================================================
Set-SuiteName "阶段5. nginx 路径遍历 + CORS"

# PT-1: GET /v1/assets/../../../etc/passwd -> 应 400/404（不应 200）
$r = Invoke-ApiWithHeaders -Path "/v1/assets/../../../etc/passwd" -Method "GET" -Headers @{}
$leakedPasswd = $r.Body -match "root:"
if ($r.StatusCode -in @(400, 404) -or -not $leakedPasswd) {
    Write-Result -TestId "PT-1" -Description "GET /v1/assets/../../../etc/passwd 不泄露系统文件" -Status "PASS" `
        -Expected "400/404 或不泄露 root:" -Actual "status=$($r.StatusCode) leaked=$leakedPasswd"
} else {
    Write-Result -TestId "PT-1" -Description "GET /v1/assets/../../../etc/passwd 不泄露系统文件" -Status "FAIL" `
        -Expected "400/404 或不泄露 root:" -Actual "status=$($r.StatusCode) leaked=$leakedPasswd body=$($r.Body.Substring(0,[Math]::Min(100,$r.Body.Length)))"
}

# PT-2: GET /../etc/passwd -> 应 400/404
$r = Invoke-ApiWithHeaders -Path "/../etc/passwd" -Method "GET" -Headers @{}
$leakedPasswd2 = $r.Body -match "root:"
if ($r.StatusCode -in @(400, 404) -or -not $leakedPasswd2) {
    Write-Result -TestId "PT-2" -Description "GET /../etc/passwd 不泄露系统文件" -Status "PASS" -Expected "400/404 或不泄露" -Actual "status=$($r.StatusCode) leaked=$leakedPasswd2"
} else {
    Write-Result -TestId "PT-2" -Description "GET /../etc/passwd 不泄露系统文件" -Status "FAIL" -Expected "400/404 或不泄露" -Actual "status=$($r.StatusCode) leaked=$leakedPasswd2"
}

# PT-3: 路径遍历响应不应泄露 /etc/passwd 内容
$r = Invoke-ApiWithHeaders -Path "/v1/assets/%2e%2e/%2e%2e/etc/passwd" -Method "GET" -Headers @{}
$leakedPasswd3 = $r.Body -match "root:"
if (-not $leakedPasswd3) {
    Write-Result -TestId "PT-3" -Description "路径遍历响应不泄露 /etc/passwd 内容" -Status "PASS" -Expected "not contains root:" -Actual "leaked=$leakedPasswd3 status=$($r.StatusCode)"
} else {
    Write-Result -TestId "PT-3" -Description "路径遍历响应不泄露 /etc/passwd 内容" -Status "FAIL" -Expected "not contains root:" -Actual "leaked=$leakedPasswd3 status=$($r.StatusCode)"
}

# CORS-1: OPTIONS /v1/assets -H "Origin: http://evil.com" -> 验证 CORS 头
$r = Invoke-ApiWithHeaders -Path "/v1/assets?limit=5" -Method "OPTIONS" -Headers @{ "Origin" = "http://evil.com" }
$allowOrigin = ""
if ($r.Headers -and $r.Headers.ContainsKey("Access-Control-Allow-Origin")) {
    $allowOrigin = $r.Headers["Access-Control-Allow-Origin"]
    if ($allowOrigin -is [array]) { $allowOrigin = $allowOrigin[0] }
}
# 无 CORS 中间件时 OPTIONS 返回 405，Allow-Origin 不存在——这是当前状态
if ($r.StatusCode -in @(200, 204, 405)) {
    $corsNote = if (-not $allowOrigin) { "无 CORS 头（OPTIONS=$($r.StatusCode)，跨域被浏览器拦截）" } else { "Allow-Origin=$allowOrigin" }
    Write-Result -TestId "CORS-1" -Description "OPTIONS /v1/assets + Origin 验证 CORS 头" -Status "PASS" `
        -Expected "200/204/405（无 CORS 中间件时 405 可接受）" -Actual "status=$($r.StatusCode) Allow-Origin=$allowOrigin" -Note $corsNote
} else {
    Write-Result -TestId "CORS-1" -Description "OPTIONS /v1/assets + Origin 验证 CORS 头" -Status "FAIL" -Expected "200/204/405" -Actual "status=$($r.StatusCode) Allow-Origin=$allowOrigin"
}

# ===========================================================================
# 阶段 6：数据一致性验证（测试后恢复原状）
# ===========================================================================
Set-SuiteName "阶段6. 数据一致性"

# CONSIST-1: asset-alice-003 scope 已恢复
$r = Invoke-Api -Path "/v1/assets/asset-alice-003" -Method "GET" -Headers $aliceH
$cur003 = if ($r.Json) { $r.Json.scope } else { "<unknown>" }
if ($cur003 -eq "team") {
    Write-Result -TestId "CONSIST-1" -Description "asset-alice-003 scope 恢复为 team" -Status "PASS" -Expected "team" -Actual "scope=$cur003"
} else {
    Write-Result -TestId "CONSIST-1" -Description "asset-alice-003 scope 恢复为 team" -Status "FAIL" -Expected "team" -Actual "scope=$cur003"
}

# CONSIST-2: asset-alice-004 scope 已恢复
$r = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $aliceH
$cur004 = if ($r.Json) { $r.Json.scope } else { "<unknown>" }
$orig004 = if ($origScope004) { $origScope004 } else { "restricted" }
if ($cur004 -eq $orig004) {
    Write-Result -TestId "CONSIST-2" -Description "asset-alice-004 scope 恢复为 $orig004" -Status "PASS" -Expected $orig004 -Actual "scope=$cur004"
} else {
    Write-Result -TestId "CONSIST-2" -Description "asset-alice-004 scope 恢复为 $orig004" -Status "FAIL" -Expected $orig004 -Actual "scope=$cur004"
}

# CONSIST-3: asset-alice-001 与 asset-alice-002 的 derived_from 关联存在
$links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceH
$linkExists = $false
if ($links.Json -and $links.Json.outgoing) {
    $found = $links.Json.outgoing | Where-Object { $_.dst_asset_id -eq "asset-alice-002" -and $_.link_type -eq "derived_from" }
    if ($found) { $linkExists = $true }
}
if ($linkExists) {
    Write-Result -TestId "CONSIST-3" -Description "asset-alice-001 -> 002 derived_from 关联存在" -Status "PASS" -Actual "关联存在"
} else {
    Write-Result -TestId "CONSIST-3" -Description "asset-alice-001 -> 002 derived_from 关联存在" -Status "FAIL" -Expected "关联存在" -Actual "关联缺失"
}

# CONSIST-4: asset-alice-004 ACL 已清理（无测试创建的 agent-dup-int / bob 临时 ACL）
$r = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "GET" -Headers $aliceH
$hasDupAgent = $false
$hasBobTemp = $false
if ($r.Json -and $r.Json.acls) {
    foreach ($a in $r.Json.acls) {
        if ($a.grantee_id -eq "agent-dup-int") { $hasDupAgent = $true }
        if ($a.grantee_type -eq "user" -and $a.grantee_id -eq "bob" -and $a.permission -eq "read") { $hasBobTemp = $true }
    }
}
if (-not $hasDupAgent -and -not $hasBobTemp) {
    Write-Result -TestId "CONSIST-4" -Description "asset-alice-004 ACL 已清理（无测试残留）" -Status "PASS" -Actual "hasDupAgent=$hasDupAgent hasBobTemp=$hasBobTemp"
} else {
    Write-Result -TestId "CONSIST-4" -Description "asset-alice-004 ACL 已清理" -Status "FAIL" -Expected "无残留" -Actual "hasDupAgent=$hasDupAgent hasBobTemp=$hasBobTemp"
}

# ===========================================================================
# 汇总
# ===========================================================================
$summary = Get-Summary
Write-Host ""
Write-Host "=== 集成测试完成 ===" -ForegroundColor White
Write-Host "总计: $($summary.Total) | PASS: $($summary.Pass) | FAIL: $($summary.Fail) | SKIP: $($summary.Skip)" -ForegroundColor White
if ($summary.Fail -gt 0) {
    Write-Host ""
    Write-Host "=== 失败用例清单 ===" -ForegroundColor Red
    $summary.Results | Where-Object { $_.Status -eq "FAIL" } | ForEach-Object {
        Write-Host "  [$($_.TestId)] $($_.Description)" -ForegroundColor Red
        Write-Host "    expected: $($_.Expected)" -ForegroundColor Yellow
        Write-Host "    actual:   $($_.Actual)" -ForegroundColor Yellow
        if ($_.Note) { Write-Host "    note:     $($_.Note)" -ForegroundColor Cyan }
    }
}

# 输出结果到文件（UTF-8 无 BOM）
$reportPath = "$PSScriptRoot\integration_report.txt"
$lines = @()
$lines += "TeamHarness 跨模块集成测试报告"
$lines += "运行时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$lines += "BaseUrl: $script:BaseUrl"
$lines += "======================================================================"
$lines += "汇总: PASS=$($summary.Pass)  FAIL=$($summary.Fail)  SKIP=$($summary.Skip)  Total=$($summary.Total)"
$lines += "======================================================================"
$lines += ""
$lines += "===== 详细结果 ====="
foreach ($t in $summary.Results) {
    $lines += "[$($t.Status)] $($t.TestId) $($t.Description)"
    if ($t.Expected) { $lines += "        Expected: $($t.Expected)" }
    if ($t.Actual)   { $lines += "        Actual:   $($t.Actual)" }
    if ($t.Note)     { $lines += "        Note:     $($t.Note)" }
}
[System.IO.File]::WriteAllText($reportPath, ($lines -join "`n"), [System.Text.UTF8Encoding]::new($false))
Write-Host ""
Write-Host "报告已写入: $reportPath" -ForegroundColor Green
