# test_concurrency.ps1 - 并发测试
# 覆盖：并行创建关联（唯一约束）、并行修改 scope、并行创建 ACL（唯一约束）
# 使用 Start-Job 实现真正的并行 HTTP 请求

. "$PSScriptRoot\common.ps1"

Set-SuiteName "2. 并发测试"

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
# 并行 HTTP 请求执行器
# 每个 request spec: @{ Path; Method; Body; Headers }
# 返回每个请求的 @{ StatusCode; Error }
# ---------------------------------------------------------------------------
function Invoke-ParallelRequests {
    param([array]$Requests, [int]$TimeoutSec = 30)

    $scriptBlock = {
        param($BaseUrl, $Path, $Method, $BodyJson, $HeadersHash)
        try {
            $params = @{
                Uri             = "$BaseUrl$Path"
                Method          = $Method
                TimeoutSec      = 15
                UseBasicParsing = $true
                ErrorAction     = "Stop"
            }
            if ($HeadersHash) { $params.Headers = $HeadersHash }
            if ($BodyJson) {
                $params.Body = $BodyJson
                $params.ContentType = "application/json"
            }
            $response = Invoke-WebRequest @params
            return [PSCustomObject]@{ StatusCode = [int]$response.StatusCode; Error = $null; Body = $response.Content }
        } catch {
            $statusCode = 0
            $errorBody = ""
            if ($_.Exception.Response) {
                try {
                    $statusCode = [int]$_.Exception.Response.StatusCode
                    $stream = $_.Exception.Response.GetResponseStream()
                    $reader = New-Object System.IO.StreamReader($stream)
                    $errorBody = $reader.ReadToEnd()
                    $reader.Close()
                } catch {}
            }
            return [PSCustomObject]@{ StatusCode = $statusCode; Error = $_.Exception.Message; Body = $errorBody }
        }
    }

    $jobs = @()
    foreach ($req in $Requests) {
        $bodyJson = if ($req.Body) {
            if ($req.Body -is [string]) { $req.Body } else { $req.Body | ConvertTo-Json -Compress -Depth 10 }
        } else { $null }
        $headersHash = if ($req.Headers) { $req.Headers } else { @{} }
        $jobs += Start-Job -ScriptBlock $scriptBlock -ArgumentList $script:BaseUrl, $req.Path, $req.Method, $bodyJson, $headersHash
    }

    # 等待所有 job 完成
    $null = $jobs | Wait-Job -Timeout $TimeoutSec
    $results = @()
    foreach ($j in $jobs) {
        $r = Receive-Job -Job $j -ErrorAction SilentlyContinue
        if ($r) { $results += $r }
        else { $results += [PSCustomObject]@{ StatusCode = -1; Error = "job timeout or empty"; Body = "" } }
    }
    $jobs | Remove-Job -Force
    return $results
}

# ---------------------------------------------------------------------------
# C1: 5 个并行请求创建相同关联（asset-alice-003 derived_from asset-alice-005）
# 预期：1 个成功，4 个因唯一约束失败
# ---------------------------------------------------------------------------
function Test-C1 {
    # 先确认 asset-alice-003 没有到 asset-alice-005 的 derived_from 关联
    $existing = Invoke-Api -Path "/v1/assets/asset-alice-003/links" -Method "GET" -Headers $aliceHeaders
    if ($existing.Json) {
        $hasLink = $existing.Json.outgoing | Where-Object {
            $_.dst_asset_id -eq "asset-alice-005" -and $_.link_type -eq "derived_from"
        }
        if ($hasLink) {
            # 删除已有关联以便测试
            Invoke-Api -Path "/v1/assets/asset-alice-003/links/$($hasLink.link_id)" -Method "DELETE" -Headers $aliceHeaders | Out-Null
            Write-Host "  [setup] 删除已有关联 $($( $hasLink.link_id ))" -ForegroundColor DarkGray
        }
    }

    # 构造 5 个并行请求
    $body = @{ dst_asset_id = "asset-alice-005"; link_type = "derived_from" } | ConvertTo-Json -Compress
    $requests = 1..5 | ForEach-Object {
        @{ Path = "/v1/assets/asset-alice-003/links"; Method = "POST"; Body = $body; Headers = $aliceHeaders }
    }

    Write-Host "  [run] 并行 5 个创建关联请求..." -ForegroundColor DarkGray
    $results = Invoke-ParallelRequests -Requests $requests

    $statusCodes = $results | ForEach-Object { $_.StatusCode }
    $successCount = ($statusCodes | Where-Object { $_ -eq 200 }).Count
    $failCount = ($statusCodes | Where-Object { $_ -ne 200 }).Count

    Write-Host "  [result] status codes: $($statusCodes -join ', ')" -ForegroundColor DarkGray
    Write-Host "  [result] success=$successCount fail=$failCount" -ForegroundColor DarkGray

    # 清理：删除成功创建的关联
    if ($successCount -ge 1) {
        $linksAfter = Invoke-Api -Path "/v1/assets/asset-alice-003/links" -Method "GET" -Headers $aliceHeaders
        if ($linksAfter.Json) {
            $created = $linksAfter.Json.outgoing | Where-Object {
                $_.dst_asset_id -eq "asset-alice-005" -and $_.link_type -eq "derived_from"
            }
            foreach ($link in $created) {
                Invoke-Api -Path "/v1/assets/asset-alice-003/links/$($link.link_id)" -Method "DELETE" -Headers $aliceHeaders | Out-Null
            }
        }
    }

    if ($successCount -eq 1 -and $failCount -eq 4) {
        Write-Result -TestId "C1" -Description "并行 5 个创建相同关联，仅 1 个成功" -Status "PASS" `
            -Expected "1 成功 + 4 失败（唯一约束）" -Actual "success=$successCount, fail=$failCount, codes=[$($statusCodes -join ',')]" `
            -Note "失败请求返回码：$($statusCodes | Where-Object { $_ -ne 200 } | Select-Object -Unique | ForEach-Object { $_ })"
    } else {
        Write-Result -TestId "C1" -Description "并行 5 个创建相同关联，仅 1 个成功" -Status "FAIL" `
            -Expected "1 成功 + 4 失败" -Actual "success=$successCount, fail=$failCount, codes=[$($statusCodes -join ',')]" `
            -Note "可能存在竞态条件或唯一约束未生效"
    }
}
Test-C1

# ---------------------------------------------------------------------------
# C2: 3 个并行请求修改同一资产 scope（asset-alice-003）
# 预期：最后一个生效，无数据损坏
# ---------------------------------------------------------------------------
function Test-C2 {
    # 记录原始 scope
    $before = Invoke-Api -Path "/v1/assets/asset-alice-003" -Method "GET" -Headers $aliceHeaders
    $origScope = if ($before.Json) { $before.Json.scope } else { "team" }
    Write-Host "  [setup] asset-alice-003 原始 scope=$origScope" -ForegroundColor DarkGray

    # 3 个并行请求，分别设为 private/public/restricted
    $scopes = @("private", "public", "restricted")
    $requests = $scopes | ForEach-Object {
        $body = @{ scope = $_ } | ConvertTo-Json -Compress
        @{ Path = "/v1/assets/asset-alice-003/scope"; Method = "PATCH"; Body = $body; Headers = $aliceHeaders }
    }

    Write-Host "  [run] 并行 3 个修改 scope 请求（private/public/restricted）..." -ForegroundColor DarkGray
    $results = Invoke-ParallelRequests -Requests $requests

    $statusCodes = $results | ForEach-Object { $_.StatusCode }
    Write-Host "  [result] status codes: $($statusCodes -join ', ')" -ForegroundColor DarkGray

    # 检查最终状态
    $after = Invoke-Api -Path "/v1/assets/asset-alice-003" -Method "GET" -Headers $aliceHeaders
    $finalScope = if ($after.Json) { $after.Json.scope } else { "unknown" }
    Write-Host "  [result] 最终 scope=$finalScope" -ForegroundColor DarkGray

    # 清理：恢复原始 scope
    $restoreBody = @{ scope = $origScope }
    Invoke-Api -Path "/v1/assets/asset-alice-003/scope" -Method "PATCH" -Body $restoreBody -Headers $aliceHeaders | Out-Null
    Write-Host "  [cleanup] 已恢复 scope 为 $origScope" -ForegroundColor DarkGray

    # 验证：所有请求返回 200，最终 scope 是三个之一，资产未损坏
    $allOk = ($statusCodes | ForEach-Object { $_ -eq 200 }) -notcontains $false
    $scopeValid = $scopes -contains $finalScope
    $assetIntact = $after.Json -and $after.Json.id -eq "asset-alice-003"

    if ($allOk -and $scopeValid -and $assetIntact) {
        Write-Result -TestId "C2" -Description "并行 3 个修改 scope，最后生效无损坏" -Status "PASS" `
            -Expected "全部 200，最终 scope 是三个之一，资产完整" -Actual "codes=[$($statusCodes -join ',')], final_scope=$finalScope, asset_id=$($after.Json.id)"
    } else {
        Write-Result -TestId "C2" -Description "并行 3 个修改 scope，最后生效无损坏" -Status "FAIL" `
            -Expected "全部 200，scope 是 private/public/restricted 之一" `
            -Actual "allOk=$allOk, scopeValid=$scopeValid, final=$finalScope, codes=[$($statusCodes -join ',')]"
    }
}
Test-C2

# ---------------------------------------------------------------------------
# C3: 5 个并行请求给同一资产加相同 ACL（asset-alice-003, user:bob, read）
# 预期：1 个成功，4 个因唯一约束失败
# ---------------------------------------------------------------------------
function Test-C3 {
    # 先确认 asset-alice-003 没有 bob 的 read ACL
    $existing = Invoke-Api -Path "/v1/assets/asset-alice-003/acl" -Method "GET" -Headers $aliceHeaders
    if ($existing.Json -and $existing.Json.acls) {
        $hasAcl = $existing.Json.acls | Where-Object {
            $_.grantee_type -eq "user" -and $_.grantee_id -eq "bob" -and $_.permission -eq "read"
        }
        if ($hasAcl) {
            Invoke-Api -Path "/v1/assets/asset-alice-003/acl/$($hasAcl.acl_id)" -Method "DELETE" -Headers $aliceHeaders | Out-Null
            Write-Host "  [setup] 删除已有 ACL" -ForegroundColor DarkGray
        }
    }

    $body = @{ grantee_type = "user"; grantee_id = "bob"; permission = "read"; granted_by = "alice" } | ConvertTo-Json -Compress
    $requests = 1..5 | ForEach-Object {
        @{ Path = "/v1/assets/asset-alice-003/acl"; Method = "POST"; Body = $body; Headers = $aliceHeaders }
    }

    Write-Host "  [run] 并行 5 个创建相同 ACL 请求..." -ForegroundColor DarkGray
    $results = Invoke-ParallelRequests -Requests $requests

    $statusCodes = $results | ForEach-Object { $_.StatusCode }
    $successCount = ($statusCodes | Where-Object { $_ -eq 200 }).Count
    $failCount = ($statusCodes | Where-Object { $_ -ne 200 }).Count

    Write-Host "  [result] status codes: $($statusCodes -join ', ')" -ForegroundColor DarkGray
    Write-Host "  [result] success=$successCount fail=$failCount" -ForegroundColor DarkGray

    # 清理：删除成功创建的 ACL
    $aclAfter = Invoke-Api -Path "/v1/assets/asset-alice-003/acl" -Method "GET" -Headers $aliceHeaders
    if ($aclAfter.Json -and $aclAfter.Json.acls) {
        $created = $aclAfter.Json.acls | Where-Object {
            $_.grantee_type -eq "user" -and $_.grantee_id -eq "bob" -and $_.permission -eq "read"
        }
        foreach ($acl in $created) {
            Invoke-Api -Path "/v1/assets/asset-alice-003/acl/$($acl.acl_id)" -Method "DELETE" -Headers $aliceHeaders | Out-Null
        }
    }

    if ($successCount -eq 1 -and $failCount -eq 4) {
        Write-Result -TestId "C3" -Description "并行 5 个创建相同 ACL，仅 1 个成功" -Status "PASS" `
            -Expected "1 成功 + 4 失败（唯一约束）" -Actual "success=$successCount, fail=$failCount, codes=[$($statusCodes -join ',')]"
    } else {
        Write-Result -TestId "C3" -Description "并行 5 个创建相同 ACL，仅 1 个成功" -Status "FAIL" `
            -Expected "1 成功 + 4 失败" -Actual "success=$successCount, fail=$failCount, codes=[$($statusCodes -join ',')]" `
            -Note "唯一约束 uq_asset_acl_grantee 可能未生效或存在竞态"
    }
}
Test-C3

Get-Summary | Out-Null
