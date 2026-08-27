# test_permissions.ps1 - 多用户权限边界测试
# 覆盖：private/team/restricted 资产的跨用户访问、owner 校验、ACL 授权
# 前置：服务运行在 http://localhost:8080，测试数据已加载

. "$PSScriptRoot\common.ps1"

Set-SuiteName "1. 多用户权限边界"

if (-not (Wait-ServiceReady)) {
    Write-Host "FATAL: 服务未就绪" -ForegroundColor Red
    exit 1
}

# 颁发 API Key（3 个用户）
$aliceKey = Issue-ApiKey -MemberId "alice" -AgentId "agent-alice"
$bobKey   = Issue-ApiKey -MemberId "bob"   -AgentId "agent-bob"
$charlieKey = Issue-ApiKey -MemberId "charlie" -AgentId "agent-charlie"

if (-not $aliceKey -or -not $bobKey -or -not $charlieKey) {
    Write-Host "FATAL: API Key 颁发失败" -ForegroundColor Red
    exit 1
}

$aliceHeaders = @{ "X-API-Key" = $aliceKey }
$bobHeaders   = @{ "X-API-Key" = $bobKey }
$charlieHeaders = @{ "X-API-Key" = $charlieKey }

Write-Host "  API Keys: alice=$($aliceKey.Substring(0,10))... bob=$($bobKey.Substring(0,10))... charlie=$($charlieKey.Substring(0,10))..." -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# P1: alice 的 private 资产，bob 查询 → 应 404 或无权限
# 注：alice 无 active private 资产，用 asset-alice-006（superseded+private）
# ---------------------------------------------------------------------------
function Test-P1 {
    $r = Invoke-Api -Path "/v1/assets/asset-alice-006" -Method "GET" -Headers $bobHeaders
    if ($r.StatusCode -eq 404 -or $r.StatusCode -eq 403) {
        Write-Result -TestId "P1" -Description "bob 查询 alice 的 private 资产应被拒绝" -Status "PASS" `
            -Expected "404 或 403" -Actual "status=$($r.StatusCode)"
    } else {
        Write-Result -TestId "P1" -Description "bob 查询 alice 的 private 资产应被拒绝" -Status "FAIL" `
            -Expected "404 或 403（非 owner 不应能访问 private 资产）" `
            -Actual "status=$($r.StatusCode)，bob 成功获取 alice 的 private 资产详情（owner=$($r.Json.owner), scope=$($r.Json.scope)）" `
            -Note "API 无鉴权中间件，get_asset 不校验 owner/scope，任何人可通过 ID 查询任意资产"
    }
}
Test-P1

# ---------------------------------------------------------------------------
# P2: alice 的 team 资产，bob 查询共享库应能看到
# ---------------------------------------------------------------------------
function Test-P2 {
    $r = Invoke-Api -Path "/v1/assets?owner=alice&scope=team&limit=50" -Method "GET" -Headers $bobHeaders
    $aliceTeamIds = @()
    if ($r.Json -and $r.Json.items) {
        $aliceTeamIds = $r.Json.items | ForEach-Object { $_.id }
    }
    $hasAliceTeam = ($aliceTeamIds -contains "asset-alice-001") -or ($aliceTeamIds -contains "asset-alice-002") -or ($aliceTeamIds -contains "asset-alice-003")
    if ($r.StatusCode -eq 200 -and $hasAliceTeam) {
        Write-Result -TestId "P2" -Description "bob 查询共享库应看到 alice 的 team 资产" -Status "PASS" `
            -Expected "200，alice team 资产可见" -Actual "status=200，alice team 资产：$($aliceTeamIds -join ', ')"
    } else {
        Write-Result -TestId "P2" -Description "bob 查询共享库应看到 alice 的 team 资产" -Status "FAIL" `
            -Expected "200，包含 alice 的 team 资产" -Actual "status=$($r.StatusCode)，items=$($aliceTeamIds.Count)"
    }
}
Test-P2

# ---------------------------------------------------------------------------
# P3: alice 的 restricted 资产（asset-alice-004），bob 未在 ACL 中 → 不应在共享库出现
# ---------------------------------------------------------------------------
function Test-P3 {
    $r = Invoke-Api -Path "/v1/assets?limit=100" -Method "GET" -Headers $bobHeaders
    $allIds = @()
    if ($r.Json -and $r.Json.items) {
        $allIds = $r.Json.items | ForEach-Object { $_.id }
    }
    $restrictedVisible = $allIds -contains "asset-alice-004"
    if (-not $restrictedVisible) {
        Write-Result -TestId "P3" -Description "alice 的 restricted 资产不应出现在 bob 的共享库" -Status "PASS" `
            -Expected "asset-alice-004 不在列表中" -Actual "不在列表中"
    } else {
        Write-Result -TestId "P3" -Description "alice 的 restricted 资产不应出现在 bob 的共享库" -Status "FAIL" `
            -Expected "asset-alice-004 不应出现在未授权用户的共享库列表" `
            -Actual "asset-alice-004 出现在共享库列表中（共 $($allIds.Count) 项）" `
            -Note "list_assets 不按 scope 过滤，restricted 资产对未授权用户可见"
    }
}
Test-P3

# ---------------------------------------------------------------------------
# P4: 给 bob 加 ACL read 后，bob 查询应能看到（asset-alice-004）
# 注：因 P3 已 FAIL（restricted 本就可见），此用例的 PASS 不能证明 ACL 生效
# ---------------------------------------------------------------------------
function Test-P4 {
    # 先添加 ACL
    $aclBody = @{ grantee_type = "user"; grantee_id = "bob"; permission = "read"; granted_by = "alice" }
    $addR = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $aclBody -Headers $aliceHeaders
    $createdAclId = if ($addR.Json) { $addR.Json.acl_id } else { $null }

    # bob 查询详情
    $r = Invoke-Api -Path "/v1/assets/asset-alice-004" -Method "GET" -Headers $bobHeaders

    # 清理：删除测试创建的 ACL
    if ($createdAclId) {
        Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$createdAclId" -Method "DELETE" -Headers $aliceHeaders | Out-Null
    }

    if ($r.StatusCode -eq 200) {
        Write-Result -TestId "P4" -Description "加 ACL read 后 bob 可查询 alice 的 restricted 资产" -Status "PASS" `
            -Expected "200" -Actual "status=$($r.StatusCode)" `
            -Note "但此 PASS 无法证明 ACL 真正生效——因 API 无鉴权，bob 加 ACL 前即可访问任意资产"
    } else {
        Write-Result -TestId "P4" -Description "加 ACL read 后 bob 可查询 alice 的 restricted 资产" -Status "FAIL" `
            -Expected "200" -Actual "status=$($r.StatusCode)"
    }
}
Test-P4

# ---------------------------------------------------------------------------
# P5: bob 尝试修改 alice 的资产 scope → 应被拒绝（owner 校验）
# 使用 asset-alice-003（alice 的 team 资产）
# ---------------------------------------------------------------------------
function Test-P5 {
    # 记录原始 scope
    $before = Invoke-Api -Path "/v1/assets/asset-alice-003" -Method "GET" -Headers $aliceHeaders
    $origScope = if ($before.Json) { $before.Json.scope } else { "team" }

    # bob 尝试修改为 public
    $body = @{ scope = "public" }
    $r = Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body $body -Headers $bobHeaders

    if ($r.StatusCode -eq 403 -or $r.StatusCode -eq 401) {
        Write-Result -TestId "P5" -Description "bob 修改 alice 资产 scope 应被拒绝" -Status "PASS" `
            -Expected "403 或 401（owner 校验）" -Actual "status=$($r.StatusCode)"
    } else {
        # 清理：恢复原始 scope
        $restoreBody = @{ scope = $origScope }
        Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body $restoreBody -Headers $aliceHeaders | Out-Null
        Write-Result -TestId "P5" -Description "bob 修改 alice 资产 scope 应被拒绝" -Status "FAIL" `
            -Expected "403 或 401（仅 owner 可修改 scope）" `
            -Actual "status=$($r.StatusCode)，bob 成功修改了 alice 的资产 scope（已恢复为 $origScope）" `
            -Note "update_asset_scope 不校验请求者是否为 owner，任何人都可修改任意资产 scope"
    }
}
Test-P5

# ---------------------------------------------------------------------------
# P6: bob 尝试删除 alice 的资产关联 → 应被拒绝
# 使用 asset-alice-001 的现有关联（derived_from asset-alice-002）
# ---------------------------------------------------------------------------
function Test-P6 {
    # 查询现有关联
    $links = Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "GET" -Headers $aliceHeaders
    $targetLink = $null
    if ($links.Json -and $links.Json.outgoing) {
        $targetLink = $links.Json.outgoing | Where-Object { $_.link_type -eq "derived_from" -and $_.dst_asset_id -eq "asset-alice-002" } | Select-Object -First 1
    }
    if (-not $targetLink) {
        Write-Result -TestId "P6" -Description "bob 删除 alice 资产关联应被拒绝" -Status "SKIP" `
            -Note "前置条件不满足：未找到 asset-alice-001 derived_from asset-alice-002 的关联"
        return
    }
    $linkId = $targetLink.link_id

    # bob 尝试删除
    $r = Invoke-Api -Path "/v1/assets/asset-alice-001/links/$linkId" -Method "DELETE" -Headers $bobHeaders

    if ($r.StatusCode -eq 403 -or $r.StatusCode -eq 401) {
        Write-Result -TestId "P6" -Description "bob 删除 alice 资产关联应被拒绝" -Status "PASS" `
            -Expected "403 或 401（owner 校验）" -Actual "status=$($r.StatusCode)"
    } else {
        # 清理：重建关联
        $recreateBody = @{ dst_asset_id = "asset-alice-002"; link_type = "derived_from" }
        Invoke-Api -Path "/v1/assets/asset-alice-001/links" -Method "POST" -Body $recreateBody -Headers $aliceHeaders | Out-Null
        Write-Result -TestId "P6" -Description "bob 删除 alice 资产关联应被拒绝" -Status "FAIL" `
            -Expected "403 或 401（仅 owner 可删除关联）" `
            -Actual "status=$($r.StatusCode)，bob 成功删除了 alice 的资产关联（已重建）" `
            -Note "delete_asset_link 不校验请求者是否为 owner，仅校验关联是否属于该资产"
    }
}
Test-P6

# ---------------------------------------------------------------------------
# P7: bob 尝试撤销 alice 资产的 ACL → 应被拒绝
# 使用 asset-alice-004 的现有 ACL（agent-charlie execute）
# ---------------------------------------------------------------------------
function Test-P7 {
    # 查询现有 ACL
    $acls = Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "GET" -Headers $aliceHeaders
    $targetAcl = $null
    if ($acls.Json -and $acls.Json.acls) {
        $targetAcl = $acls.Json.acls[0]
    }
    if (-not $targetAcl) {
        Write-Result -TestId "P7" -Description "bob 撤销 alice 资产 ACL 应被拒绝" -Status "SKIP" `
            -Note "前置条件不满足：asset-alice-004 无 ACL"
        return
    }
    $aclId = $targetAcl.acl_id
    $origGranteeType = $targetAcl.grantee_type
    $origGranteeId = $targetAcl.grantee_id
    $origPermission = $targetAcl.permission
    $origGrantedBy = $targetAcl.granted_by

    # bob 尝试撤销
    $r = Invoke-Api -Path "/v1/assets/asset-alice-004/acl/$aclId" -Method "DELETE" -Headers $bobHeaders

    if ($r.StatusCode -eq 403 -or $r.StatusCode -eq 401) {
        Write-Result -TestId "P7" -Description "bob 撤销 alice 资产 ACL 应被拒绝" -Status "PASS" `
            -Expected "403 或 401（owner 校验）" -Actual "status=$($r.StatusCode)"
    } else {
        # 清理：重建 ACL
        $recreateBody = @{
            grantee_type = $origGranteeType
            grantee_id   = $origGranteeId
            permission   = $origPermission
            granted_by   = $origGrantedBy
        }
        Invoke-Api -Path "/v1/assets/asset-alice-004/acl" -Method "POST" -Body $recreateBody -Headers $aliceHeaders | Out-Null
        Write-Result -TestId "P7" -Description "bob 撤销 alice 资产 ACL 应被拒绝" -Status "FAIL" `
            -Expected "403 或 401（仅 owner 可撤销 ACL）" `
            -Actual "status=$($r.StatusCode)，bob 成功撤销了 alice 资产的 ACL（已重建）" `
            -Note "delete_asset_acl 不校验请求者是否为 owner，仅校验 ACL 是否属于该资产"
    }
}
Test-P7

# ---------------------------------------------------------------------------
# P8: charlie 用自己的 key 操作自己的资产 → 正常
# ---------------------------------------------------------------------------
function Test-P8 {
    # 查询自己的资产列表
    $listR = Invoke-Api -Path "/v1/assets?owner=charlie&limit=10" -Method "GET" -Headers $charlieHeaders
    # 查询自己的统计
    $statsR = Invoke-Api -Path "/v1/members/charlie/stats" -Method "GET" -Headers $charlieHeaders
    # 查询自己的资产详情
    $detailR = Invoke-Api -Path "/v1/assets/asset-charlie-001" -Method "GET" -Headers $charlieHeaders

    $allOk = ($listR.StatusCode -eq 200) -and ($statsR.StatusCode -eq 200) -and ($detailR.StatusCode -eq 200)
    if ($allOk) {
        Write-Result -TestId "P8" -Description "charlie 操作自己的资产正常" -Status "PASS" `
            -Expected "200 x3（list/stats/detail）" `
            -Actual "list=$($listR.StatusCode), stats=$($statsR.StatusCode), detail=$($detailR.StatusCode)"
    } else {
        Write-Result -TestId "P8" -Description "charlie 操作自己的资产正常" -Status "FAIL" `
            -Expected "200 x3" -Actual "list=$($listR.StatusCode), stats=$($statsR.StatusCode), detail=$($detailR.StatusCode)"
    }
}
Test-P8

Get-Summary | Out-Null
