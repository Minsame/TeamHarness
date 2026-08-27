# test_backend.ps1 - TeamHarness 后端 API 鉴权与权限测试
# 范围：通过 HTTP 调用 http://localhost:8080/v1/* 验证鉴权/owner/scope/完整性/错误格式/边界
# 前置：docker-compose 服务运行中（nginx:8080 -> asset-service:8080）
# 复用 tests/realenv/common.ps1 的 Invoke-Api / Issue-ApiKey / Write-Result / Get-Summary
# 测试数据：alice/bob/charlie 已存在（asset-alice-001~006, asset-bob-001~004, asset-charlie-001~004）
# 测试后恢复原状，用例独立可复现

. "$PSScriptRoot\..\realenv\common.ps1"

# ===========================================================================
# 辅助：获取响应头（Invoke-Api 不返回头，需单独取 Content-Type / Allow）
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
    return @{
        StatusCode = $statusCode
        Body       = $respBody
        Headers    = $respHeaders
        Error      = $errMsg
    }
}

# ===========================================================================
# 辅助：直接通过数据库清理测试创建的 link/acl（避免依赖被测 API 清理）
# 这里仍用 API 清理（owner 自己操作），如果 API 本身不可信则手动按 ID 清理
# ===========================================================================

# ===========================================================================
# 前置：等待服务就绪 + 颁发 API Key
# ===========================================================================
Set-SuiteName "0. 前置准备"

if (-not (Wait-ServiceReady)) {
    Write-Host "FATAL: 服务未就绪（http://localhost:8080/healthz）" -ForegroundColor Red
    exit 1
}
Write-Result -TestId "PREP-0" -Description "服务就绪" -Status "PASS" -Actual "healthz=200"

$aliceKey = Issue-ApiKey -MemberId "alice" -AgentId "agent-alice"
$bobKey   = Issue-ApiKey -MemberId "bob"   -AgentId "agent-bob"
$charlieKey = Issue-ApiKey -MemberId "charlie" -AgentId "agent-charlie"

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
# 1. API Key 鉴权
# ===========================================================================
Set-SuiteName "1. API Key 鉴权"

# AUTH-1a: 有效 API Key -> 200
function Test-AUTH1a {
    $r = Invoke-Api -Path "/v1/assets?owner=alice&limit=5" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -eq 200) {
        Write-Result -TestId "AUTH-1a" -Description "有效 API Key 访问 -> 200" -Status "PASS" `
            -Expected "200" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "AUTH-1a" -Description "有效 API Key 访问 -> 200" -Status "FAIL" `
            -Expected "200" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-AUTH1a

# AUTH-1b: 无效 API Key (th_invalid) -> 应 401
function Test-AUTH1b {
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = "th_invalid" }
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "AUTH-1b" -Description "无效 API Key (th_invalid) -> 401" -Status "PASS" `
            -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "AUTH-1b" -Description "无效 API Key (th_invalid) -> 401" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-AUTH1b

# AUTH-1c: 缺失 X-API-Key 头 -> 应 401
function Test-AUTH1c {
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{}
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "AUTH-1c" -Description "缺失 X-API-Key 头 -> 401" -Status "PASS" `
            -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "AUTH-1c" -Description "缺失 X-API-Key 头 -> 401" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-AUTH1c

# AUTH-1d: 空字符串 API Key -> 应 401
function Test-AUTH1d {
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = "" }
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "AUTH-1d" -Description "空字符串 API Key -> 401" -Status "PASS" `
            -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "AUTH-1d" -Description "空字符串 API Key -> 401" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-AUTH1d

# AUTH-1e: 吊销的 API Key -> 应 401（issue -> revoke -> 用 revoked key 访问）
function Test-AUTH1e {
    # 颁发新 key
    $issued = Issue-ApiKey -MemberId "alice" -AgentId "agent-revoked-test"
    if (-not $issued) {
        Write-Result -TestId "AUTH-1e" -Description "吊销的 API Key -> 401" -Status "SKIP" -Note "前置失败：无法颁发测试 key"
        return
    }
    # 查 key_id（通过 lookup 不返回 key_id，用 list_keys 不行；用 /v1/auth/apikey 返回 key_id）
    $issueBody = @{ member_id = "alice"; agent_id = "agent-revoked-test" }
    $issueR = Invoke-Api -Path "/v1/auth/apikey" -Method "POST" -Body $issueBody
    # 上面 Issue-ApiKey 已颁发一次，再颁发会得到不同 key。这里用 issueR 的 key_id 来 revoke
    if (-not $issueR.Json -or -not $issueR.Json.key_id) {
        Write-Result -TestId "AUTH-1e" -Description "吊销的 API Key -> 401" -Status "SKIP" -Note "无法获取 key_id"
        return
    }
    $keyId = $issueR.Json.key_id
    $keyToRevoke = $issueR.Json.api_key
    # 吊销
    $revR = Invoke-Api -Path "/v1/auth/apikey/rotate" -Method "POST" -Body @{ key_id = $keyId }
    # 注：rotate 会颁发新 key 并使旧 key 失效（status=rotated）。用旧 key 访问应 401
    # （没有直接的 revoke 端点，用 rotate 代替——旧 key 同样失效）
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = $keyToRevoke }
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "AUTH-1e" -Description "轮换后的旧 API Key -> 401" -Status "PASS" `
            -Expected "401" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "AUTH-1e" -Description "轮换后的旧 API Key -> 401" -Status "FAIL" `
            -Expected "401（旧 key 应失效）" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-AUTH1e

# AUTH-1f: bob 用自己的 key 访问 alice 的 private 资产 asset-bob-002(bob private) -> alice 查应 404
# （此用例验证：有效 key 但 scope 不允许 -> 404，与 AUTH-1b 的 401 区分）
function Test-AUTH1f {
    # alice 用自己的 key 查 bob 的 private 资产
    $r = Invoke-Api -Path "/v1/assets/asset-bob-002" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403) {
        Write-Result -TestId "AUTH-1f" -Description "alice 查 bob 的 private 资产 -> 404/403" -Status "PASS" `
            -Expected "404 或 403（scope 访问控制）" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "AUTH-1f" -Description "alice 查 bob 的 private 资产 -> 404/403" -Status "FAIL" `
            -Expected "404 或 403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-AUTH1f

# ===========================================================================
# 2. /v1/auth/apikey/lookup 行为（设计意图验证）
# ===========================================================================
Set-SuiteName "2. apikey/lookup 行为"

# LOOKUP-1: 无效 key -> 200 + agent_id=null（设计意图：反查接口，非鉴权失败）
function Test-LOOKUP1 {
    $r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = "th_invalid" }
    if ($r.StatusCode -eq 200 -and $r.Json -and $null -eq $r.Json.agent_id) {
        Write-Result -TestId "LOOKUP-1" -Description "无效 key lookup -> 200 + agent_id=null" -Status "PASS" `
            -Expected "200 + agent_id=null（设计为反查接口）" -Actual "status=$($r.StatusCode) agent_id=$($r.Json.agent_id)"
    } else {
        Write-Result -TestId "LOOKUP-1" -Description "无效 key lookup -> 200 + agent_id=null" -Status "FAIL" `
            -Expected "200 + agent_id=null" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-LOOKUP1

# LOOKUP-2: 有效 key -> 200 + agent_id 有值
function Test-LOOKUP2 {
    $r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = $aliceKey }
    if ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.agent_id -eq "agent-alice") {
        Write-Result -TestId "LOOKUP-2" -Description "有效 key lookup -> 200 + agent_id" -Status "PASS" `
            -Expected "200 + agent_id=agent-alice" -Actual "status=$($r.StatusCode) agent_id=$($r.Json.agent_id)"
    } else {
        Write-Result -TestId "LOOKUP-2" -Description "有效 key lookup -> 200 + agent_id" -Status "FAIL" `
            -Expected "200 + agent_id=agent-alice" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-LOOKUP2

# LOOKUP-3: 空 key -> 200 + agent_id=null
function Test-LOOKUP3 {
    $r = Invoke-Api -Path "/v1/auth/apikey/lookup" -Method "POST" -Body @{ api_key = "" }
    if ($r.StatusCode -eq 200 -and $r.Json -and $null -eq $r.Json.agent_id) {
        Write-Result -TestId "LOOKUP-3" -Description "空 key lookup -> 200 + agent_id=null" -Status "PASS" `
            -Expected "200 + agent_id=null" -Actual "status=$($r.StatusCode) agent_id=$($r.Json.agent_id)"
    } else {
        Write-Result -TestId "LOOKUP-3" -Description "空 key lookup -> 200 + agent_id=null" -Status "FAIL" `
            -Expected "200 + agent_id=null" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-LOOKUP3

# ===========================================================================
# 3. Owner 权限校验
# ===========================================================================
Set-SuiteName "3. Owner 权限校验"

# OWNER-1: bob 改 alice 的资产 scope -> 应 403
function Test-OWNER1 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body @{ scope = "public" } -Headers $bobH
    if ($r.StatusCode -eq 403) {
        Write-Result -TestId "OWNER-1" -Description "bob 改 alice 资产 scope -> 403" -Status "PASS" `
            -Expected "403（owner 校验）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        # 清理：若实际改了，恢复
        if ($r.StatusCode -eq 200) {
            Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body @{ scope = "team" } -Headers $aliceH | Out-Null
        }
        Write-Result -TestId "OWNER-1" -Description "bob 改 alice 资产 scope -> 403" -Status "FAIL" `
            -Expected "403" -Actual "status=$($r.StatusCode) body=$($r.Body)" `
            -Note "update_asset_scope 的 _assert_owner 未生效"
    }
}
Test-OWNER1

# OWNER-2: bob 删除 alice 的资产关联 -> 应 403
# 先查 alice 的现有关联，取一个 link_id
function Test-OWNER2 {
    $links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceH
    $targetLink = $null
    if ($links.Json -and $links.Json.outgoing) {
        $targetLink = $links.Json.outgoing | Select-Object -First 1
    }
    if (-not $targetLink) {
        Write-Result -TestId "OWNER-2" -Description "bob 删除 alice 资产关联 -> 403" -Status "SKIP" `
            -Note "前置不满足：asset-alice-001 无 outgoing 关联"
        return
    }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links/$($targetLink.link_id)" -Method "DELETE" -Headers $bobH
    if ($r.StatusCode -eq 403) {
        Write-Result -TestId "OWNER-2" -Description "bob 删除 alice 资产关联 -> 403" -Status "PASS" `
            -Expected "403（owner 校验）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        # 清理：若实际删了，重建
        if ($r.StatusCode -eq 200) {
            $recreate = @{ dst_asset_id = $targetLink.dst_asset_id; link_type = $targetLink.link_type }
            Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body $recreate -Headers $aliceH | Out-Null
        }
        Write-Result -TestId "OWNER-2" -Description "bob 删除 alice 资产关联 -> 403" -Status "FAIL" `
            -Expected "403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-OWNER2

# OWNER-3: bob 撤销 alice 资产的 ACL -> 应 403
function Test-OWNER3 {
    $acls = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "GET" -Headers $aliceH
    $targetAcl = $null
    if ($acls.Json -and $acls.Json.acls -and $acls.Json.acls.Count -gt 0) {
        $targetAcl = $acls.Json.acls[0]
    }
    if (-not $targetAcl) {
        Write-Result -TestId "OWNER-3" -Description "bob 撤销 alice 资产 ACL -> 403" -Status "SKIP" `
            -Note "前置不满足：asset-alice-004 无 ACL"
        return
    }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$($targetAcl.acl_id)" -Method "DELETE" -Headers $bobH
    if ($r.StatusCode -eq 403) {
        Write-Result -TestId "OWNER-3" -Description "bob 撤销 alice 资产 ACL -> 403" -Status "PASS" `
            -Expected "403（owner 校验）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        # 清理：若实际删了，重建
        if ($r.StatusCode -eq 200) {
            $recreate = @{
                grantee_type = $targetAcl.grantee_type
                grantee_id   = $targetAcl.grantee_id
                permission   = $targetAcl.permission
                granted_by   = $targetAcl.granted_by
            }
            Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $recreate -Headers $aliceH | Out-Null
        }
        Write-Result -TestId "OWNER-3" -Description "bob 撤销 alice 资产 ACL -> 403" -Status "FAIL" `
            -Expected "403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-OWNER3

# OWNER-4: bob 创建关联到 alice 的资产（src=asset-alice-001）-> 应 403
function Test-OWNER4 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" `
        -Body @{ dst_asset_id = "asset-alice-005"; link_type = "related_to" } -Headers $bobH
    if ($r.StatusCode -eq 403) {
        Write-Result -TestId "OWNER-4" -Description "bob 以 alice 资产为 src 创建关联 -> 403" -Status "PASS" `
            -Expected "403（src 资产 owner 校验）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        # 清理：若实际创建了，删除
        if ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.link_id) {
            Invoke-Api -Path "/v1/assets/asset-alice-001/links/$($r.Json.link_id)" -Method "DELETE" -Headers $aliceH | Out-Null
        }
        Write-Result -TestId "OWNER-4" -Description "bob 以 alice 资产为 src 创建关联 -> 403" -Status "FAIL" `
            -Expected "403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-OWNER4

# OWNER-5: alice 操作自己的资产 -> 应 200
function Test-OWNER5 {
    # 记录原 scope
    $before = Invoke-Api -Path "/v1/assets/asset-alice-005" -Method "GET" -Headers $aliceH
    $origScope = if ($before.Json) { $before.Json.scope } else { "public" }
    # alice 改自己的 scope 为 team 再改回
    $r = Invoke-Api -Path "/v1/assets/asset-alice-005/scope" -Method "PATCH" -Body @{ scope = "team" } -Headers $aliceH
    # 恢复
    Invoke-Api -Path "/v1/assets/asset-alice-005/scope" -Method "PATCH" -Body @{ scope = $origScope } -Headers $aliceH | Out-Null
    if ($r.StatusCode -eq 200) {
        Write-Result -TestId "OWNER-5" -Description "alice 改自己资产 scope -> 200" -Status "PASS" `
            -Expected "200" -Actual "status=$($r.StatusCode)（已恢复 scope=$origScope）"
    } else {
        Write-Result -TestId "OWNER-5" -Description "alice 改自己资产 scope -> 200" -Status "FAIL" `
            -Expected "200" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-OWNER5

# ===========================================================================
# 4. Scope 访问控制
# ===========================================================================
Set-SuiteName "4. Scope 访问控制"

# SCOPE-1: alice 查 bob 的 private 资产 asset-bob-002 -> 应 404
function Test-SCOPE1 {
    $r = Invoke-Api -Path "/v1/assets/asset-bob-002" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403) {
        Write-Result -TestId "SCOPE-1" -Description "alice 查 bob 的 private 资产 -> 404/403" -Status "PASS" `
            -Expected "404 或 403（private 仅 owner 可见）" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "SCOPE-1" -Description "alice 查 bob 的 private 资产 -> 404/403" -Status "FAIL" `
            -Expected "404 或 403" -Actual "status=$($r.StatusCode) body=$($r.Body)" `
            -Note "_can_view_asset 对 private 非 owner 未拒绝"
    }
}
Test-SCOPE1

# SCOPE-2: bob 查 alice 的 restricted 资产 asset-alice-004（无 user ACL）-> 应 404
function Test-SCOPE2 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $bobH
    if ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403) {
        Write-Result -TestId "SCOPE-2" -Description "bob 查 alice 的 restricted 资产（无 ACL）-> 404/403" -Status "PASS" `
            -Expected "404 或 403（restricted 仅 ACL 授权用户可见）" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "SCOPE-2" -Description "bob 查 alice 的 restricted 资产（无 ACL）-> 404/403" -Status "FAIL" `
            -Expected "404 或 403" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-SCOPE2

# SCOPE-3: alice 给 bob 加 ACL read 后，bob 查 asset-alice-004 -> 应 200（测试后清理）
function Test-SCOPE3 {
    $aclBody = @{ grantee_type = "user"; grantee_id = "bob"; permission = "read"; granted_by = "alice" }
    $addR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $aclBody -Headers $aliceH
    $createdAclId = if ($addR.Json) { $addR.Json.acl_id } else { $null }

    $r = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $bobH

    # 清理
    if ($createdAclId) {
        Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceH | Out-Null
    }

    if ($r.StatusCode -eq 200) {
        Write-Result -TestId "SCOPE-3" -Description "加 ACL read 后 bob 可查 alice 的 restricted 资产 -> 200" -Status "PASS" `
            -Expected "200（ACL 授权生效）" -Actual "status=$($r.StatusCode)（ACL 已清理）"
    } else {
        Write-Result -TestId "SCOPE-3" -Description "加 ACL read 后 bob 可查 alice 的 restricted 资产 -> 200" -Status "FAIL" `
            -Expected "200" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-SCOPE3

# SCOPE-4: bob 查 /v1/assets?scope=team -> 不应包含 alice 的 private/restricted
function Test-SCOPE4 {
    $r = Invoke-Api -Path "/v1/assets?scope=team&limit=200" -Method "GET" -Headers $bobH
    $ids = @()
    if ($r.Json -and $r.Json.items) {
        $ids = $r.Json.items | ForEach-Object { $_.id }
    }
    $leaked = $ids | Where-Object { $_ -like "asset-alice-*" }
    # alice 的 team 资产（001/002/003）可以出现，private/restricted（004/006）不应出现
    $badLeak = $leaked | Where-Object { $_ -eq "asset-alice-004" -or $_ -eq "asset-alice-006" }
    if ($r.StatusCode -eq 200 -and $badLeak.Count -eq 0) {
        Write-Result -TestId "SCOPE-4" -Description "bob 查 scope=team 不含 alice 的 private/restricted" -Status "PASS" `
            -Expected "不含 asset-alice-004/006" -Actual "alice 资产可见：$($leaked -join ',')"
    } else {
        Write-Result -TestId "SCOPE-4" -Description "bob 查 scope=team 不含 alice 的 private/restricted" -Status "FAIL" `
            -Expected "不含 asset-alice-004/006" -Actual "status=$($r.StatusCode) 泄露：$($badLeak -join ',')"
    }
}
Test-SCOPE4

# SCOPE-5: bob 查 /v1/assets（不带 owner，共享库视图）-> 不应包含 alice 的 private/restricted
function Test-SCOPE5 {
    $r = Invoke-Api -Path "/v1/assets?limit=200" -Method "GET" -Headers $bobH
    $ids = @()
    if ($r.Json -and $r.Json.items) {
        $ids = $r.Json.items | ForEach-Object { $_.id }
    }
    $hasAlicePrivate = $ids -contains "asset-alice-006"
    $hasAliceRestricted = $ids -contains "asset-alice-004"
    if ($r.StatusCode -eq 200 -and -not $hasAlicePrivate -and -not $hasAliceRestricted) {
        Write-Result -TestId "SCOPE-5" -Description "共享库视图不含 alice 的 private/restricted" -Status "PASS" `
            -Expected "不含 asset-alice-004/006" -Actual "列表共 $($ids.Count) 项，无泄露"
    } else {
        Write-Result -TestId "SCOPE-5" -Description "共享库视图不含 alice 的 private/restricted" -Status "FAIL" `
            -Expected "不含 asset-alice-004/006" `
            -Actual "status=$($r.StatusCode) has004=$hasAliceRestricted has006=$hasAlicePrivate"
    }
}
Test-SCOPE5

# ===========================================================================
# 5. 数据完整性
# ===========================================================================
Set-SuiteName "5. 数据完整性"

# INTEG-1: 重复创建相同关联 -> 应 409
# 先确保 alice 的 001->002 derived_from 关联存在，再创建一次
function Test-INTEG1 {
    # 先查是否已存在
    $links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceH
    $existing = $null
    if ($links.Json -and $links.Json.outgoing) {
        $existing = $links.Json.outgoing | Where-Object { $_.dst_asset_id -eq "asset-alice-002" -and $_.link_type -eq "derived_from" } | Select-Object -First 1
    }
    $createdLink = $null
    if (-not $existing) {
        # 先创建
        $createR = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" `
            -Body @{ dst_asset_id = "asset-alice-002"; link_type = "derived_from" } -Headers $aliceH
        if ($createR.Json) { $createdLink = $createR.Json.link_id }
    }

    # 重复创建 -> 应 409
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" `
        -Body @{ dst_asset_id = "asset-alice-002"; link_type = "derived_from" } -Headers $aliceH

    if ($r.StatusCode -eq 409) {
        Write-Result -TestId "INTEG-1" -Description "重复创建相同关联 -> 409" -Status "PASS" `
            -Expected "409（IntegrityError 已捕获）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "INTEG-1" -Description "重复创建相同关联 -> 409" -Status "FAIL" `
            -Expected "409" -Actual "status=$($r.StatusCode) body=$($r.Body)" `
            -Note "IntegrityError 未被捕获为 409，返回 500 或其他"
    }
}
Test-INTEG1

# INTEG-2: 重复创建相同 ACL -> 应 409
function Test-INTEG2 {
    # 先创建一个 ACL（agent:agent-test-dup）
    $aclBody = @{ grantee_type = "agent"; grantee_id = "agent-test-dup"; permission = "read"; granted_by = "alice" }
    $createR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $aclBody -Headers $aliceH
    $createdAclId = if ($createR.Json) { $createR.Json.acl_id } else { $null }

    # 重复创建 -> 应 409
    $r = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $aclBody -Headers $aliceH

    # 清理
    if ($createdAclId) {
        Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceH | Out-Null
    }

    if ($r.StatusCode -eq 409) {
        Write-Result -TestId "INTEG-2" -Description "重复创建相同 ACL -> 409" -Status "PASS" `
            -Expected "409（IntegrityError 已捕获）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "INTEG-2" -Description "重复创建相同 ACL -> 409" -Status "FAIL" `
            -Expected "409" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-INTEG2

# INTEG-3: 自环关联（asset-alice-001 -> asset-alice-001）-> 应 400
function Test-INTEG3 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" `
        -Body @{ dst_asset_id = "asset-alice-001"; link_type = "related_to" } -Headers $aliceH
    if ($r.StatusCode -eq 400) {
        Write-Result -TestId "INTEG-3" -Description "自环关联 -> 400" -Status "PASS" `
            -Expected "400（不能自关联）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        # 清理：若实际创建了，删除
        if ($r.StatusCode -eq 200 -and $r.Json -and $r.Json.link_id) {
            Invoke-Api -Path "/v1/assets/asset-alice-001/links/$($r.Json.link_id)" -Method "DELETE" -Headers $aliceH | Out-Null
        }
        Write-Result -TestId "INTEG-3" -Description "自环关联 -> 400" -Status "FAIL" `
            -Expected "400" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-INTEG3

# INTEG-4: 关联到不存在的 dst 资产 -> 应 404
function Test-INTEG4 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" `
        -Body @{ dst_asset_id = "asset-not-exist-999"; link_type = "related_to" } -Headers $aliceH
    if ($r.StatusCode -eq 404) {
        Write-Result -TestId "INTEG-4" -Description "关联到不存在的 dst 资产 -> 404" -Status "PASS" `
            -Expected "404（目标资产不存在）" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "INTEG-4" -Description "关联到不存在的 dst 资产 -> 404" -Status "FAIL" `
            -Expected "404" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-INTEG4

# INTEG-5: BFS depth=0 -> 应 422（Query ge=1）
function Test-INTEG5 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=0" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -eq 422) {
        Write-Result -TestId "INTEG-5" -Description "BFS depth=0 -> 422" -Status "PASS" `
            -Expected "422（depth ge=1）" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "INTEG-5" -Description "BFS depth=0 -> 422" -Status "FAIL" `
            -Expected "422" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-INTEG5

# INTEG-6: BFS depth=4 -> 应 422（Query le=3）
function Test-INTEG6 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=4" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -eq 422) {
        Write-Result -TestId "INTEG-6" -Description "BFS depth=4 -> 422" -Status "PASS" `
            -Expected "422（depth le=3）" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "INTEG-6" -Description "BFS depth=4 -> 422" -Status "FAIL" `
            -Expected "422" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-INTEG6

# INTEG-7: BFS depth=1 -> 应 200
function Test-INTEG7 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=1" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -eq 200) {
        Write-Result -TestId "INTEG-7" -Description "BFS depth=1 -> 200" -Status "PASS" `
            -Expected "200" -Actual "status=$($r.StatusCode) nodes=$($r.Json.nodes.Count)"
    } else {
        Write-Result -TestId "INTEG-7" -Description "BFS depth=1 -> 200" -Status "FAIL" `
            -Expected "200" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-INTEG7

# INTEG-8: BFS depth=3 -> 应 200
function Test-INTEG8 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=3" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -eq 200) {
        Write-Result -TestId "INTEG-8" -Description "BFS depth=3 -> 200" -Status "PASS" `
            -Expected "200" -Actual "status=$($r.StatusCode) nodes=$($r.Json.nodes.Count)"
    } else {
        Write-Result -TestId "INTEG-8" -Description "BFS depth=3 -> 200" -Status "FAIL" `
            -Expected "200" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-INTEG8

# ===========================================================================
# 6. 错误响应格式
# ===========================================================================
Set-SuiteName "6. 错误响应格式"

# ERR-1: 401 响应为 JSON 且含 detail
function Test-ERR1 {
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{}
    $isJson = $false; $hasDetail = $false
    if ($r.Json) { $isJson = $true; if ($r.Json.detail) { $hasDetail = $true } }
    if ($r.StatusCode -eq 401 -and $isJson -and $hasDetail) {
        Write-Result -TestId "ERR-1" -Description "401 响应为 JSON 且含 detail" -Status "PASS" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "ERR-1" -Description "401 响应为 JSON 且含 detail" -Status "FAIL" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode) isJson=$isJson hasDetail=$hasDetail body=$($r.Body)"
    }
}
Test-ERR1

# ERR-2: 403 响应为 JSON 且含 detail
function Test-ERR2 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body @{ scope = "public" } -Headers $bobH
    $isJson = $false; $hasDetail = $false
    if ($r.Json) { $isJson = $true; if ($r.Json.detail) { $hasDetail = $true } }
    if ($r.StatusCode -eq 403 -and $isJson -and $hasDetail) {
        Write-Result -TestId "ERR-2" -Description "403 响应为 JSON 且含 detail" -Status "PASS" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "ERR-2" -Description "403 响应为 JSON 且含 detail" -Status "FAIL" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode) isJson=$isJson hasDetail=$hasDetail body=$($r.Body)"
    }
}
Test-ERR2

# ERR-3: 404 响应为 JSON 且含 detail
function Test-ERR3 {
    $r = Invoke-Api -Path "/v1/assets/asset-not-exist-999" -Method "GET" -Headers $aliceH
    $isJson = $false; $hasDetail = $false
    if ($r.Json) { $isJson = $true; if ($r.Json.detail) { $hasDetail = $true } }
    if ($r.StatusCode -eq 404 -and $isJson -and $hasDetail) {
        Write-Result -TestId "ERR-3" -Description "404 响应为 JSON 且含 detail" -Status "PASS" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode) detail=$($r.Json.detail)"
    } else {
        Write-Result -TestId "ERR-3" -Description "404 响应为 JSON 且含 detail" -Status "FAIL" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode) isJson=$isJson hasDetail=$hasDetail body=$($r.Body)"
    }
}
Test-ERR3

# ERR-4: 422 响应为 JSON（FastAPI 校验错误，detail 是数组）
function Test-ERR4 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/graph?depth=0" -Method "GET" -Headers $aliceH
    $isJson = $false; $hasDetail = $false
    if ($r.Json) { $isJson = $true; if ($r.Json.detail) { $hasDetail = $true } }
    if ($r.StatusCode -eq 422 -and $isJson -and $hasDetail) {
        Write-Result -TestId "ERR-4" -Description "422 响应为 JSON 且含 detail" -Status "PASS" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "ERR-4" -Description "422 响应为 JSON 且含 detail" -Status "FAIL" `
            -Expected "JSON + detail" -Actual "status=$($r.StatusCode) isJson=$isJson hasDetail=$hasDetail body=$($r.Body)"
    }
}
Test-ERR4

# ERR-5: OPTIONS 请求 -> 应 204/200，验证实际
function Test-ERR5 {
    $r = Invoke-ApiWithHeaders -Path "/v1/assets?limit=5" -Method "OPTIONS" -Headers @{}
    # FastAPI 默认不处理 OPTIONS（无 CORS 中间件）-> 405
    if ($r.StatusCode -eq 204 -or $r.StatusCode -eq 200) {
        Write-Result -TestId "ERR-5" -Description "OPTIONS 请求 -> 204/200" -Status "PASS" `
            -Expected "204 或 200（CORS 预检）" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "ERR-5" -Description "OPTIONS 请求 -> 204/200" -Status "FAIL" `
            -Expected "204 或 200" -Actual "status=$($r.StatusCode)（无 CORS 中间件，OPTIONS 被拒）" `
            -Note "app.py 未添加 CORSMiddleware，OPTIONS 返回 $($r.StatusCode)"
    }
}
Test-ERR5

# ERR-6: Content-Type 是否含 charset=utf-8
function Test-ERR6 {
    $r = Invoke-ApiWithHeaders -Path "/v1/assets?owner=alice&limit=5" -Method "GET" -Headers $aliceH
    $ct = ""
    if ($r.Headers -and $r.Headers.ContainsKey("Content-Type")) {
        $ct = $r.Headers["Content-Type"]
        if ($ct -is [array]) { $ct = $ct[0] }
    }
    $hasCharset = $ct -like "*charset=utf-8*" -or $ct -like "*charset=UTF-8*"
    if ($hasCharset) {
        Write-Result -TestId "ERR-6" -Description "Content-Type 含 charset=utf-8" -Status "PASS" `
            -Expected "charset=utf-8" -Actual "Content-Type=$ct"
    } else {
        Write-Result -TestId "ERR-6" -Description "Content-Type 含 charset=utf-8" -Status "FAIL" `
            -Expected "charset=utf-8" -Actual "Content-Type=$ct" `
            -Note "FastAPI 默认 application/json 不带 charset"
    }
}
Test-ERR6

# ===========================================================================
# 7. 边界值
# ===========================================================================
Set-SuiteName "7. 边界值"

# EDGE-1: 超长 API Key（10000 字符）-> 不应崩溃，应 401
function Test-EDGE1 {
    $longKey = "th_" + ("a" * 9997)
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = $longKey }
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "EDGE-1" -Description "超长 API Key（10000 字符）-> 401 不崩溃" -Status "PASS" `
            -Expected "401" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "EDGE-1" -Description "超长 API Key（10000 字符）-> 401 不崩溃" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-EDGE1

# EDGE-2: SQL 注入尝试 -> 不应崩溃
function Test-EDGE2 {
    $injectKey = "th_' OR 1=1 --"
    $r = Invoke-Api -Path "/v1/assets?limit=5" -Method "GET" -Headers @{ "X-API-Key" = $injectKey }
    if ($r.StatusCode -eq 401) {
        Write-Result -TestId "EDGE-2" -Description "SQL 注入 API Key -> 401 不崩溃" -Status "PASS" `
            -Expected "401（参数化查询防注入）" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "EDGE-2" -Description "SQL 注入 API Key -> 401 不崩溃" -Status "FAIL" `
            -Expected "401" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-EDGE2

# EDGE-3: XSS 尝试（asset_id 含 <script>）-> 不应崩溃
function Test-EDGE3 {
    $r = Invoke-Api -Path "/v1/assets/%3Cscript%3Ealert(1)%3C%2Fscript%3E" -Method "GET" -Headers $aliceH
    # 应返回 404（资产不存在）或 422（路径校验），不应 500
    if ($r.StatusCode -in @(404, 422, 400)) {
        Write-Result -TestId "EDGE-3" -Description "XSS asset_id -> 4xx 不崩溃" -Status "PASS" `
            -Expected "404/422/400" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "EDGE-3" -Description "XSS asset_id -> 4xx 不崩溃" -Status "FAIL" `
            -Expected "4xx" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-EDGE3

# EDGE-4: 路径参数特殊字符（../ 尝试路径穿越）-> 不应崩溃
function Test-EDGE4 {
    $r = Invoke-Api -Path "/v1/assets/..%2F..%2Fetc%2Fpasswd" -Method "GET" -Headers $aliceH
    if ($r.StatusCode -in @(404, 422, 400)) {
        Write-Result -TestId "EDGE-4" -Description "路径穿越 asset_id -> 4xx 不崩溃" -Status "PASS" `
            -Expected "404/422/400" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "EDGE-4" -Description "路径穿越 asset_id -> 4xx 不崩溃" -Status "FAIL" `
            -Expected "4xx" -Actual "status=$($r.StatusCode) body=$($r.Body)"
    }
}
Test-EDGE4

# ===========================================================================
# 汇总
# ===========================================================================
$summary = Get-Summary
Write-Host ""
Write-Host "=== 测试完成 ===" -ForegroundColor White
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
