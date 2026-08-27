// TeamHarness 多账号并发测试
// 工具：Playwright 多 browser context + fetch API
// 运行：node tests/multi_user/test_multi_user.spec.mjs
//
// 测试范围：5 个账号（alice/bob/charlie/dave/eve）
//   1. 权限隔离（private/restricted/team/public + ACL + owner 篡改防护 + member stats 篡改 + 前端 UI）
//   2. 并发操作（并发修改 scope / 并发创建关联 / 并发读 / scope 实时影响 / ACL 撤销实时影响）
//   3. 跨用户协作（关联创建→对端可见 / 图谱跨用户 BFS / ACL 添加→对端可见 / 关联删除→对端消失）
//   4. 数据一致性（共享库 total / 图谱对称性 / 分页一致性）
//
// 测试铁律：覆盖正常+异常+并发；用例独立；可复现；round-trip 还原；FAIL 不掩盖

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE = 'http://localhost:8080/';
const FRONTEND = BASE;

const KEYS = {
  alice:   'th_b8df89f8075ccf5e58a34673292d2653',
  bob:     'th_3209616b587f35283ae03d6222d2a556',
  charlie: 'th_91e7b4462a614481ad66e4a5891922a9',
  dave:    'th_457e67873d4f92a0bb687915e4f00ec7',
  eve:     'th_a7ac0374ed0792e6c186a72f9490ca31',
};

const SCREEN_DIR = path.join(__dirname, 'screenshots');
if (!fs.existsSync(SCREEN_DIR)) fs.mkdirSync(SCREEN_DIR, { recursive: true });

// 初始状态快照（用于测试后恢复）
// 来自 test_data.json + 实际 API 探查（运行前确认过）
const INITIAL = {
  // alice-003 scope 初始 = team（2.1 改 public、2.4 改 private 后必须还原）
  alice003_scope: 'team',
  // bob-001 scope 初始 = team（2.1 改 public 后必须还原）
  bob001_scope: 'team',
  // alice-001 scope 初始 = restricted（不改）
  alice001_scope: 'restricted',
  // alice-001 ACL 初始 = bob(read) + charlie(execute)
  alice001_acl: [
    { grantee_id: 'bob',     permission: 'read'    },
    { grantee_id: 'charlie', permission: 'execute' },
  ],
  // 4 条原始跨用户关联（src, type, dst）
  original_links: [
    { src: 'asset-alice-001',   type: 'derived_from', dst: 'asset-bob-001'    },
    { src: 'asset-bob-001',     type: 'related_to',    dst: 'asset-charlie-001' },
    { src: 'asset-charlie-002', type: 'supersedes',    dst: 'asset-alice-005'  },
    { src: 'asset-alice-005',   type: 'related_to',    dst: 'asset-bob-004'    },
  ],
};

// ---------------- 测试结果收集 ----------------
const results = [];
let passCount = 0, failCount = 0, skipCount = 0;
function record(name, status, detail = '') {
  results.push({ name, status, detail });
  if (status === 'PASS') passCount++;
  else if (status === 'FAIL') failCount++;
  else skipCount++;
  const tag = status === 'PASS' ? '\u001b[32m[PASS]\u001b[0m'
    : status === 'FAIL' ? '\u001b[31m[FAIL]\u001b[0m'
    : '\u001b[33m[SKIP]\u001b[0m';
  console.log(`${tag} ${name}${detail ? ' — ' + detail : ''}`);
}

// ---------------- API helper（fetch）----------------
// 统一封装：返回 { status, ok, body }
async function apiCall(method, urlPath, apiKey, body = null) {
  const headers = { 'X-API-Key': apiKey };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const opts = { method, headers };
  if (body !== null) opts.body = JSON.stringify(body);
  const resp = await fetch(BASE + urlPath.replace(/^\//, ''), opts);
  let parsed = null;
  const text = await resp.text();
  if (text) {
    try { parsed = JSON.parse(text); }
    catch { parsed = text; }
  }
  return { status: resp.status, ok: resp.ok, body: parsed };
}

// 便捷封装
const api = {
  getAsset: (id, key)        => apiCall('GET',    `/v1/assets/${id}`, key),
  listAssets: (qs, key)     => apiCall('GET',    `/v1/assets${qs}`, key),
  patchScope: (id, scope, key) => apiCall('PATCH', `/v1/assets/${id}/scope`, key, { scope }),
  getStats: (id, key)       => apiCall('GET',    `/v1/members/${id}/stats`, key),
  getLinks: (id, key)       => apiCall('GET',    `/v1/assets/${id}/links`, key),
  createLink: (id, dst, type, key) => apiCall('POST',   `/v1/assets/${id}/links`, key, { dst_asset_id: dst, link_type: type }),
  deleteLink: (id, linkId, key) => apiCall('DELETE', `/v1/assets/${id}/links/${linkId}`, key),
  getGraph: (id, depth, key) => apiCall('GET',    `/v1/assets/${id}/graph?depth=${depth}`, key),
  getAcl: (id, key)         => apiCall('GET',    `/v1/assets/${id}/acl`, key),
  createAcl: (id, granteeType, granteeId, perm, key) =>
    apiCall('POST', `/v1/assets/${id}/acl`, key, { grantee_type: granteeType, grantee_id: granteeId, permission: perm, granted_by: '' }),
  deleteAcl: (id, aclId, key) => apiCall('DELETE', `/v1/assets/${id}/acl/${aclId}`, key),
};

// ---------------- Playwright helper ----------------
async function shot(page, filename) {
  try { await page.screenshot({ path: path.join(SCREEN_DIR, filename), fullPage: true }); }
  catch (e) { /* 截图失败不致命 */ }
}

async function loginAs(browser, member, apiKey) {
  // 返回 {ctx, page, error}：失败时不抛错，让调用方在 page 上截图
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  try {
    await page.goto(FRONTEND, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForSelector('.login-card', { timeout: 15000 });
    const memberInput = page.locator('input[placeholder="如：alice"]').first();
    try {
      await memberInput.waitFor({ state: 'visible', timeout: 15000 });
    } catch (e) {
      if (pageErrors.length > 0) {
        throw new Error(`前端未挂载（pageerror 阻塞 Vue/Element Plus）：${pageErrors.join(' | ')}`);
      }
      throw e;
    }
    await memberInput.fill(member);
    const keyInput = page.locator('input[placeholder="th_ 开头"]').first();
    await keyInput.waitFor({ state: 'visible', timeout: 10000 });
    await keyInput.fill(apiKey);
    await page.locator('.el-button', { hasText: '登录' }).first().click();
    await page.waitForSelector('.app-header', { timeout: 15000 });
    await page.waitForTimeout(1000);
    return { ctx, page, error: null };
  } catch (e) {
    return { ctx, page, error: e };
  }
}

async function gotoMenu(page, menuText) {
  const item = page.locator('.el-menu-item', { hasText: menuText }).first();
  await item.click();
  await page.waitForTimeout(700);
}

async function waitTableLoaded(page, timeout = 8000) {
  await page.waitForTimeout(300);
  try { await page.waitForSelector('.el-loading-mask', { state: 'hidden', timeout }); }
  catch { /* 可能没有 loading mask */ }
  await page.waitForTimeout(200);
}

// ---------------- 恢复辅助 ----------------
// 在 finally 块调用：尽力恢复到 INITIAL 状态，不抛错（只记录）
async function restoreState() {
  console.log('\n--- 恢复数据原状 ---');
  const aliceKey = KEYS.alice, bobKey = KEYS.bob, charlieKey = KEYS.charlie;

  // 1. 恢复 scope
  try {
    const r1 = await api.getAsset('asset-alice-003', aliceKey);
    if (r1.status === 200 && r1.body.scope !== INITIAL.alice003_scope) {
      await api.patchScope('asset-alice-003', INITIAL.alice003_scope, aliceKey);
      console.log(`  alice-003 scope: ${r1.body.scope} -> ${INITIAL.alice003_scope}`);
    } else {
      console.log(`  alice-003 scope: 已是 ${INITIAL.alice003_scope}（无需恢复）`);
    }
  } catch (e) { console.log('  恢复 alice-003 scope 失败：', e.message); }

  try {
    const r2 = await api.getAsset('asset-bob-001', bobKey);
    if (r2.status === 200 && r2.body.scope !== INITIAL.bob001_scope) {
      await api.patchScope('asset-bob-001', INITIAL.bob001_scope, bobKey);
      console.log(`  bob-001 scope: ${r2.body.scope} -> ${INITIAL.bob001_scope}`);
    } else {
      console.log(`  bob-001 scope: 已是 ${INITIAL.bob001_scope}（无需恢复）`);
    }
  } catch (e) { console.log('  恢复 bob-001 scope 失败：', e.message); }

  // 2. 恢复 alice-001 ACL：先查现状，对比 INITIAL
  try {
    const r = await api.getAcl('asset-alice-001', aliceKey);
    if (r.status === 200) {
      const current = r.body.acls || [];
      // 删除多出来的（如 dave）
      for (const a of current) {
        if (!INITIAL.alice001_acl.find(x => x.grantee_id === a.grantee_id && x.permission === a.permission)) {
          await api.deleteAcl('asset-alice-001', a.acl_id, aliceKey);
          console.log(`  删除多余 ACL：${a.grantee_id}(${a.permission})`);
        }
      }
      // 补回缺失的（如 bob 被 2.5 删除后未补回）
      const currentGrants = current.map(a => `${a.grantee_id}:${a.permission}`);
      for (const target of INITIAL.alice001_acl) {
        if (!currentGrants.includes(`${target.grantee_id}:${target.permission}`)) {
          // 重新查询，避免刚补回的又被判定缺失
          const r2 = await api.getAcl('asset-alice-001', aliceKey);
          const cur2 = (r2.body.acls || []).map(a => `${a.grantee_id}:${a.permission}`);
          if (!cur2.includes(`${target.grantee_id}:${target.permission}`)) {
            await api.createAcl('asset-alice-001', 'user', target.grantee_id, target.permission, aliceKey);
            console.log(`  补回缺失 ACL：${target.grantee_id}(${target.permission})`);
          }
        }
      }
      console.log('  alice-001 ACL 恢复完成');
    }
  } catch (e) { console.log('  恢复 alice-001 ACL 失败：', e.message); }

  // 3. 恢复跨用户关联：确保 4 条原始关联存在，删除测试期间新增的非原始关联
  try {
    // 收集每个 src 资产当前的所有正向关联
    const srcAssets = [...new Set(INITIAL.original_links.map(l => l.src))];
    const liveLinks = []; // [{src, link_id, type, dst}]
    for (const src of srcAssets) {
      const owner = src.startsWith('asset-alice') ? aliceKey : src.startsWith('asset-bob') ? bobKey : charlieKey;
      const r = await api.getLinks(src, owner);
      if (r.status === 200) {
        for (const o of r.body.outgoing) {
          liveLinks.push({ src, link_id: o.link_id, type: o.link_type, dst: o.dst_asset_id });
        }
      }
    }
    // 删除非原始关联（alice-003 → charlie-001 / bob-003 → charlie-001 等）
    for (const link of liveLinks) {
      const isOriginal = INITIAL.original_links.some(
        ol => ol.src === link.src && ol.type === link.type && ol.dst === link.dst
      );
      if (!isOriginal) {
        const owner = link.src.startsWith('asset-alice') ? aliceKey : link.src.startsWith('asset-bob') ? bobKey : charlieKey;
        await api.deleteLink(link.src, link.link_id, owner);
        console.log(`  删除非原始关联：${link.src} --[${link.type}]--> ${link.dst}`);
      }
    }
    // 补回缺失的原始关联（如 3.4 删除 alice-001 → bob-001 后未补回）
    for (const ol of INITIAL.original_links) {
      const exists = liveLinks.some(l => l.src === ol.src && l.type === ol.type && l.dst === ol.dst);
      if (!exists) {
        // 再次检查，可能上一轮已补
        const owner = ol.src.startsWith('asset-alice') ? aliceKey : ol.src.startsWith('asset-bob') ? bobKey : charlieKey;
        const r = await api.getLinks(ol.src, owner);
        const stillMissing = !(r.body?.outgoing || []).some(o => o.link_type === ol.type && o.dst_asset_id === ol.dst);
        if (stillMissing) {
          await api.createLink(ol.src, ol.dst, ol.type, owner);
          console.log(`  补回缺失关联：${ol.src} --[${ol.type}]--> ${ol.dst}`);
        }
      }
    }
    console.log('  跨用户关联恢复完成');
  } catch (e) { console.log('  恢复跨用户关联失败：', e.message); }
}

// =====================================================================
// 主流程
// =====================================================================
(async () => {
  console.log('=== TeamHarness 多账号并发测试开始 ===\n');
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    console.error('浏览器启动失败：', e.message);
    process.exit(2);
  }

  try {
    // ==================================================================
    // 1. 权限隔离测试（API）
    // ==================================================================
    console.log('=== 1. 权限隔离测试 ===\n');

    // ---- 1.1 private 资产隔离 ----
    console.log('--- 1.1 private 资产隔离 ---');
    {
      const id = 'asset-alice-002';
      const checks = [
        { user: 'alice',   key: KEYS.alice,   expect: 200 },
        { user: 'bob',     key: KEYS.bob,     expect: 404 },
        { user: 'charlie', key: KEYS.charlie, expect: 404 },
        { user: 'dave',    key: KEYS.dave,    expect: 404 },
      ];
      let allPass = true;
      const details = [];
      for (const c of checks) {
        const r = await api.getAsset(id, c.key);
        const pass = r.status === c.expect;
        if (!pass) allPass = false;
        details.push(`${c.user}=${r.status}(期望${c.expect})`);
      }
      record('1.1 private 资产隔离（alice-002）', allPass ? 'PASS' : 'FAIL', details.join(', '));
    }

    // ---- 1.2 restricted 资产 + ACL ----
    console.log('--- 1.2 restricted 资产 + ACL ---');
    {
      const id = 'asset-alice-001';
      const checks = [
        { user: 'alice',   key: KEYS.alice,   expect: 200, note: 'owner' },
        { user: 'bob',     key: KEYS.bob,     expect: 200, note: 'ACL read' },
        { user: 'charlie', key: KEYS.charlie, expect: 200, note: 'ACL execute' },
        { user: 'dave',    key: KEYS.dave,    expect: 404, note: '无 ACL' },
        { user: 'eve',     key: KEYS.eve,     expect: 404, note: '无 ACL' },
      ];
      let allPass = true;
      const details = [];
      for (const c of checks) {
        const r = await api.getAsset(id, c.key);
        const pass = r.status === c.expect;
        if (!pass) allPass = false;
        details.push(`${c.user}=${r.status}(期望${c.expect},${c.note})`);
      }
      record('1.2 restricted + ACL（alice-001）', allPass ? 'PASS' : 'FAIL', details.join(', '));
    }

    // ---- 1.3 team/public 资产共享 ----
    console.log('--- 1.3 team/public 资产共享 ---');
    {
      const targets = [
        { id: 'asset-alice-003', scope: 'team',   owner: 'alice' },
        { id: 'asset-alice-005', scope: 'public', owner: 'alice' },
        { id: 'asset-bob-004',   scope: 'public', owner: 'bob'   },
      ];
      const users = ['alice', 'bob', 'charlie', 'dave', 'eve'];
      let allPass = true;
      const details = [];
      for (const t of targets) {
        for (const u of users) {
          const r = await api.getAsset(t.id, KEYS[u]);
          if (r.status !== 200) allPass = false;
          details.push(`${u}->${t.id}=${r.status}`);
        }
      }
      record('1.3 team/public 共享（3 资产 × 5 用户全 200）', allPass ? 'PASS' : 'FAIL', details.join(', '));
    }

    // ---- 1.4 owner 参数篡改防护（STATE-1 回归）----
    console.log('--- 1.4 owner 参数篡改防护 ---');
    {
      const cases = [
        { user: 'alice', key: KEYS.alice, qs: '?owner=bob',   expect: 403, note: 'alice 查 bob 的个人库' },
        { user: 'bob',   key: KEYS.bob,   qs: '?owner=alice', expect: 403, note: 'bob 查 alice 的个人库' },
        { user: 'alice', key: KEYS.alice, qs: '?owner=alice', expect: 200, note: 'alice 查自己的' },
        { user: 'alice', key: KEYS.alice, qs: '',              expect: 200, note: 'alice 查共享库（不传 owner）' },
      ];
      let allPass = true;
      const details = [];
      for (const c of cases) {
        const r = await api.listAssets(c.qs, c.key);
        if (r.status !== c.expect) allPass = false;
        details.push(`${c.user}${c.qs || '(no owner)'}=${r.status}(期望${c.expect})`);
      }
      record('1.4 owner 参数篡改防护', allPass ? 'PASS' : 'FAIL', details.join(', '));
    }

    // ---- 1.5 member stats 篡改防护 ----
    console.log('--- 1.5 member stats 篡改防护 ---');
    {
      const cases = [
        { caller: 'alice', key: KEYS.alice, target: 'bob',   expect: 403, note: 'alice 查 bob stats' },
        { caller: 'bob',   key: KEYS.bob,   target: 'alice', expect: 403, note: 'bob 查 alice stats' },
        { caller: 'alice', key: KEYS.alice, target: 'alice', expect: 200, note: 'alice 查自己 stats' },
      ];
      let allPass = true;
      const details = [];
      for (const c of cases) {
        const r = await api.getStats(c.target, c.key);
        if (r.status !== c.expect) allPass = false;
        details.push(`${c.caller}->/members/${c.target}/stats=${r.status}(期望${c.expect})`);
      }
      record('1.5 member stats 篡改防护', allPass ? 'PASS' : 'FAIL', details.join(', '));
    }

    // ---- 1.6 前端 UI 权限隔离（Playwright）----
    console.log('--- 1.6 前端 UI 权限隔离 ---');
    // 1.6.1 alice 登录 → 我的规则库 → 应看到 5 个资产，含 alice-001（restricted）
    {
      let ctx, page;
      try {
        const r = await loginAs(browser, 'alice', KEYS.alice);
        ctx = r.ctx; page = r.page;
        if (r.error) throw r.error;
        await waitTableLoaded(page);
        const rows = await page.locator('.app-main .el-table__row').count();
        const text = await page.locator('.app-main .table-container').first().textContent();
        const has001 = text.includes('asset-alice-001');
        const has002 = text.includes('asset-alice-002');
        if (rows === 5 && has001 && has002) {
          record('1.6.1 alice 我的规则库（5 资产，含 restricted）', 'PASS', `行数=${rows}`);
        } else {
          throw new Error(`行数=${rows}, has001=${has001}, has002=${has002}`);
        }
        await shot(page, '1-6-1-alice-my.png');
      } catch (e) {
        if (page) await shot(page, '1-6-1-alice-my-fail.png');
        record('1.6.1 alice 我的规则库（5 资产，含 restricted）', 'FAIL', e.message);
      } finally {
        if (ctx) await ctx.close();
      }
    }
    // 1.6.2 bob 登录 → 共享库 → 看到 team/public，看不到 alice 的 private/restricted
    {
      let ctx, page;
      try {
        const r = await loginAs(browser, 'bob', KEYS.bob);
        ctx = r.ctx; page = r.page;
        if (r.error) throw r.error;
        await gotoMenu(page, '共享库');
        await waitTableLoaded(page);
        const text = await page.locator('.app-main .table-container').first().textContent();
        const has003 = text.includes('asset-alice-003'); // team
        const has005 = text.includes('asset-alice-005'); // public
        const has002 = text.includes('asset-alice-002'); // private - 不应出现
        const has001 = text.includes('asset-alice-001'); // restricted - 不应出现
        if (has003 && has005 && !has002 && !has001) {
          record('1.6.2 bob 共享库（见 alice team/public，不见 private/restricted）', 'PASS');
        } else {
          throw new Error(`has003=${has003}, has005=${has005}, has002=${has002}(应 false), has001=${has001}(应 false)`);
        }
        await shot(page, '1-6-2-bob-shared.png');
      } catch (e) {
        if (page) await shot(page, '1-6-2-bob-shared-fail.png');
        record('1.6.2 bob 共享库（见 alice team/public，不见 private/restricted）', 'FAIL', e.message);
      } finally {
        if (ctx) await ctx.close();
      }
    }
    // 1.6.3 dave 登录 → 我的规则库 → 空状态
    {
      let ctx, page;
      try {
        const r = await loginAs(browser, 'dave', KEYS.dave);
        ctx = r.ctx; page = r.page;
        if (r.error) throw r.error;
        await waitTableLoaded(page);
        const rows = await page.locator('.app-main .el-table__row').count();
        const emptyText = await page.locator('.app-main').first().textContent();
        const isEmpty = rows === 0 && (emptyText.includes('暂无') || emptyText.includes('No Data') || emptyText.includes('空'));
        if (isEmpty) {
          record('1.6.3 dave 我的规则库（空状态）', 'PASS', `rows=${rows}`);
        } else {
          throw new Error(`rows=${rows}, text snippet=${emptyText.slice(0, 200)}`);
        }
        await shot(page, '1-6-3-dave-empty.png');
      } catch (e) {
        if (page) await shot(page, '1-6-3-dave-empty-fail.png');
        record('1.6.3 dave 我的规则库（空状态）', 'FAIL', e.message);
      } finally {
        if (ctx) await ctx.close();
      }
    }

    // ==================================================================
    // 2. 并发操作测试
    // ==================================================================
    console.log('\n=== 2. 并发操作测试 ===\n');

    // ---- 2.1 并发修改各自 scope（不冲突）----
    console.log('--- 2.1 并发修改各自 scope（不冲突）---');
    {
      try {
        // 并发：alice 改 alice-003 → public，同时 bob 改 bob-001 → public
        const [r1, r2] = await Promise.all([
          api.patchScope('asset-alice-003', 'public', KEYS.alice),
          api.patchScope('asset-bob-001',   'public', KEYS.bob),
        ]);
        const bothOk = r1.status === 200 && r2.status === 200;
        // 验证互不干扰：alice-003 应为 public，bob-001 应为 public
        const [v1, v2] = await Promise.all([
          api.getAsset('asset-alice-003', KEYS.alice),
          api.getAsset('asset-bob-001', KEYS.bob),
        ]);
        const scopeOk = v1.body.scope === 'public' && v2.body.scope === 'public';
        if (bothOk && scopeOk) {
          record('2.1 并发修改各自 scope', 'PASS', `alice-003=${v1.body.scope}, bob-001=${v2.body.scope}`);
        } else {
          throw new Error(`r1=${r1.status} r2=${r2.status} v1.scope=${v1.body?.scope} v2.scope=${v2.body?.scope}`);
        }
        // 恢复会在 finally/restoreState 中完成
      } catch (e) {
        record('2.1 并发修改各自 scope', 'FAIL', e.message);
      }
    }

    // ---- 2.2 并发创建关联到同一资产 ----
    console.log('--- 2.2 并发创建关联到同一资产 ---');
    {
      // alice 创建 alice-003 → charlie-001（related_to）
      // bob   创建 bob-003   → charlie-001（related_to）
      // 两请求并发；因为 src 不同，UniqueConstraint(src,dst,type) 不冲突，都应成功
      // 验证：两个都 200；清理创建的关联
      const createdLinks = []; // [{src, link_id, owner_key}]
      try {
        const [r1, r2] = await Promise.all([
          api.createLink('asset-alice-003', 'asset-charlie-001', 'related_to', KEYS.alice),
          api.createLink('asset-bob-003',   'asset-charlie-001', 'related_to', KEYS.bob),
        ]);
        if (r1.status === 200 && r1.body?.link_id) createdLinks.push({ src: 'asset-alice-003', link_id: r1.body.link_id, owner_key: KEYS.alice });
        if (r2.status === 200 && r2.body?.link_id) createdLinks.push({ src: 'asset-bob-003',   link_id: r2.body.link_id, owner_key: KEYS.bob });
        // 接受：两个都成功（不同 src） 或 一个 200 + 一个 409（同 src 同 type 才会 409，本场景不应）
        const both200 = r1.status === 200 && r2.status === 200;
        const mixed = (r1.status === 200 || r2.status === 200) && (r1.status === 409 || r2.status === 409);
        if (both200 || mixed) {
          record('2.2 并发创建关联到 charlie-001', 'PASS', `alice-003→=${r1.status}, bob-003→=${r2.status}`);
        } else {
          throw new Error(`r1=${r1.status} r2=${r2.status}`);
        }
      } catch (e) {
        record('2.2 并发创建关联到 charlie-001', 'FAIL', e.message);
      } finally {
        // 清理：删除本次创建的关联（恢复原始 4 条关联状态）
        for (const link of createdLinks) {
          try { await api.deleteLink(link.src, link.link_id, link.owner_key); }
          catch (e) { console.log(`  清理关联失败：${link.src}/${link.link_id}: ${e.message}`); }
        }
      }
    }

    // ---- 2.3 并发查看同一资产 ----
    console.log('--- 2.3 并发查看同一资产（asset-alice-005 public）---');
    {
      try {
        const id = 'asset-alice-005';
        const promises = Object.entries(KEYS).map(([u, k]) => api.getAsset(id, k));
        const rs = await Promise.all(promises);
        const all200 = rs.every(r => r.status === 200);
        // 内容一致：所有响应 body.id 相同
        const ids = new Set(rs.map(r => r.body?.id));
        const consistent = ids.size === 1 && [...ids][0] === id;
        if (all200 && consistent) {
          record('2.3 并发查看 asset-alice-005', 'PASS', `5 用户全 200，内容一致`);
        } else {
          throw new Error(`all200=${all200}, consistent=${consistent}, statuses=${rs.map(r=>r.status).join(',')}`);
        }
      } catch (e) {
        record('2.3 并发查看 asset-alice-005', 'FAIL', e.message);
      }
    }

    // ---- 2.4 scope 修改实时影响 ----
    console.log('--- 2.4 scope 修改实时影响（alice-003 team→private）---');
    {
      try {
        // 前置：确保 alice-003 是 team（应为初始状态，但 2.1 改成了 public，先恢复为 team）
        await api.patchScope('asset-alice-003', 'team', KEYS.alice);
        // bob 先查 alice-003 → 200（team 共享）
        const before = await api.getAsset('asset-alice-003', KEYS.bob);
        if (before.status !== 200) throw new Error(`前置：bob 查 team 状态的 alice-003 应 200，实际 ${before.status}`);
        // alice 改为 private
        const r = await api.patchScope('asset-alice-003', 'private', KEYS.alice);
        if (r.status !== 200) throw new Error(`alice 改 scope 失败：${r.status}`);
        // bob 立即查 → 404
        const mid = await api.getAsset('asset-alice-003', KEYS.bob);
        // alice 改回 team
        await api.patchScope('asset-alice-003', 'team', KEYS.alice);
        // bob 再查 → 200
        const after = await api.getAsset('asset-alice-003', KEYS.bob);
        if (mid.status === 404 && after.status === 200) {
          record('2.4 scope 实时影响', 'PASS', `team→private: bob ${before.status}→${mid.status}; private→team: bob→${after.status}`);
        } else {
          throw new Error(`mid=${mid.status}(期望 404), after=${after.status}(期望 200)`);
        }
      } catch (e) {
        record('2.4 scope 实时影响', 'FAIL', e.message);
      }
    }

    // ---- 2.5 ACL 撤销实时影响 ----
    console.log('--- 2.5 ACL 撤销实时影响（alice-001 bob read）---');
    {
      let revokedAclId = null;
      try {
        // 前置：查 alice-001 当前 ACL，找到 bob 的 acl_id
        const aclRes = await api.getAcl('asset-alice-001', KEYS.alice);
        if (aclRes.status !== 200) throw new Error(`查 ACL 失败：${aclRes.status}`);
        const bobAcl = (aclRes.body.acls || []).find(a => a.grantee_id === 'bob' && a.permission === 'read');
        if (!bobAcl) throw new Error('前置：bob 的 read ACL 不存在，无法测试撤销');
        revokedAclId = bobAcl.acl_id;
        // bob 先查 alice-001 → 200
        const before = await api.getAsset('asset-alice-001', KEYS.bob);
        if (before.status !== 200) throw new Error(`前置：bob 查 alice-001 应 200，实际 ${before.status}`);
        // alice 撤销 bob 的 ACL
        const del = await api.deleteAcl('asset-alice-001', revokedAclId, KEYS.alice);
        if (del.status !== 200) throw new Error(`撤销 ACL 失败：${del.status}`);
        revokedAclId = null; // 已删除
        // bob 立即查 → 404
        const mid = await api.getAsset('asset-alice-001', KEYS.bob);
        // alice 重新授权 bob read
        const readd = await api.createAcl('asset-alice-001', 'user', 'bob', 'read', KEYS.alice);
        if (readd.status !== 200) throw new Error(`重新授权失败：${readd.status}`);
        // bob 再查 → 200
        const after = await api.getAsset('asset-alice-001', KEYS.bob);
        if (mid.status === 404 && after.status === 200) {
          record('2.5 ACL 撤销实时影响', 'PASS', `撤销: bob ${before.status}→${mid.status}; 重授: bob→${after.status}`);
        } else {
          throw new Error(`mid=${mid.status}(期望 404), after=${after.status}(期望 200)`);
        }
      } catch (e) {
        record('2.5 ACL 撤销实时影响', 'FAIL', e.message);
      }
    }

    // ==================================================================
    // 3. 跨用户协作测试
    // ==================================================================
    console.log('\n=== 3. 跨用户协作测试 ===\n');

    // ---- 3.1 关联创建 → 对端可见 ----
    console.log('--- 3.1 关联创建 → 对端可见（alice-003 → bob-003）---');
    {
      let createdLinkId = null;
      try {
        // alice 创建 alice-003 → bob-003（related_to）
        const r = await api.createLink('asset-alice-003', 'asset-bob-003', 'related_to', KEYS.alice);
        if (r.status !== 200) throw new Error(`创建关联失败：${r.status}`);
        createdLinkId = r.body.link_id;
        // bob 查自己的 links（incoming）→ 应看到 alice-003 的反向关联
        const r2 = await api.getLinks('asset-bob-003', KEYS.bob);
        if (r2.status !== 200) throw new Error(`bob 查 links 失败：${r2.status}`);
        const incoming = r2.body.incoming || [];
        const found = incoming.find(i => i.src_asset_id === 'asset-alice-003' && i.link_type === 'related_to');
        if (found) {
          record('3.1 关联创建→对端可见', 'PASS', `bob incoming 含 alice-003 --related_to--> bob-003`);
        } else {
          throw new Error(`bob incoming 未找到 alice-003 的反向关联：${JSON.stringify(incoming.map(i=>({src:i.src_asset_id,type:i.link_type})))}`);
        }
      } catch (e) {
        record('3.1 关联创建→对端可见', 'FAIL', e.message);
      } finally {
        // 清理
        if (createdLinkId) {
          try { await api.deleteLink('asset-alice-003', createdLinkId, KEYS.alice); }
          catch (e) { console.log('  清理 3.1 关联失败：', e.message); }
        }
      }
    }

    // ---- 3.2 图谱跨用户 BFS 遍历 ----
    console.log('--- 3.2 图谱跨用户 BFS depth=3（alice-001 出发）---');
    {
      try {
        // alice-001 → bob-001 (derived_from) → charlie-001 (related_to)
        // 从 alice-001 BFS depth=3 应能覆盖 alice-001, bob-001, charlie-001
        const r = await api.getGraph('asset-alice-001', 3, KEYS.alice);
        if (r.status !== 200) throw new Error(`BFS 失败：${r.status}`);
        const nodeIds = (r.body.nodes || []).map(n => n.id);
        const edges = r.body.edges || [];
        const hasAlice = nodeIds.includes('asset-alice-001');
        const hasBob   = nodeIds.includes('asset-bob-001');
        const hasCharl = nodeIds.includes('asset-charlie-001');
        const edgeTypes = edges.map(e => e.link_type);
        const hasDerived = edgeTypes.includes('derived_from');
        const hasRelated = edgeTypes.includes('related_to');
        if (hasAlice && hasBob && hasCharl && hasDerived && hasRelated) {
          record('3.2 图谱跨用户 BFS depth=3', 'PASS', `节点=[${nodeIds.join(',')}], 边类型=[${[...new Set(edgeTypes)].join(',')}]`);
        } else {
          throw new Error(`节点缺失或边类型缺失：alice=${hasAlice} bob=${hasBob} charlie=${hasCharl} derived=${hasDerived} related=${hasRelated}`);
        }
      } catch (e) {
        record('3.2 图谱跨用户 BFS depth=3', 'FAIL', e.message);
      }
    }

    // ---- 3.3 ACL 添加 → 对端立即可见 ----
    console.log('--- 3.3 ACL 添加→对端立即可见（alice 给 dave 授 alice-001）---');
    {
      let createdAclId = null;
      try {
        // 前置：dave 查 alice-001 → 404（无 ACL）
        const before = await api.getAsset('asset-alice-001', KEYS.dave);
        if (before.status !== 404) throw new Error(`前置：dave 查 alice-001 应 404，实际 ${before.status}`);
        // alice 给 dave 授 read
        const r = await api.createAcl('asset-alice-001', 'user', 'dave', 'read', KEYS.alice);
        if (r.status !== 200) throw new Error(`授权失败：${r.status}`);
        createdAclId = r.body.acl_id;
        // dave 立即查 → 200
        const mid = await api.getAsset('asset-alice-001', KEYS.dave);
        // alice 撤销 dave 的 ACL
        const del = await api.deleteAcl('asset-alice-001', createdAclId, KEYS.alice);
        if (del.status !== 200) throw new Error(`撤销失败：${del.status}`);
        createdAclId = null;
        // dave 再查 → 404
        const after = await api.getAsset('asset-alice-001', KEYS.dave);
        if (mid.status === 200 && after.status === 404) {
          record('3.3 ACL 添加→对端可见', 'PASS', `dave ${before.status}→授权→${mid.status}→撤销→${after.status}`);
        } else {
          throw new Error(`mid=${mid.status}(期望 200), after=${after.status}(期望 404)`);
        }
      } catch (e) {
        record('3.3 ACL 添加→对端可见', 'FAIL', e.message);
      } finally {
        // 兜底：如果创建后未撤销，撤销
        if (createdAclId) {
          try { await api.deleteAcl('asset-alice-001', createdAclId, KEYS.alice); }
          catch (e) { console.log('  兜底撤销 dave ACL 失败：', e.message); }
        }
      }
    }

    // ---- 3.4 关联删除 → 对端消失 ----
    console.log('--- 3.4 关联删除→对端消失（alice-001 → bob-001 derived_from）---');
    {
      let deletedLinkId = null;
      try {
        // 前置：查 alice-001 的 outgoing，找到 → bob-001 的 derived_from 关联
        const r = await api.getLinks('asset-alice-001', KEYS.alice);
        if (r.status !== 200) throw new Error(`查 alice-001 links 失败：${r.status}`);
        const link = (r.body.outgoing || []).find(o => o.dst_asset_id === 'asset-bob-001' && o.link_type === 'derived_from');
        if (!link) throw new Error('前置：alice-001 → bob-001 derived_from 关联不存在');
        deletedLinkId = link.link_id;
        // bob 先查自己 incoming → 应有 alice-001 的关联
        const before = await api.getLinks('asset-bob-001', KEYS.bob);
        const foundBefore = (before.body.incoming || []).find(i => i.src_asset_id === 'asset-alice-001' && i.link_type === 'derived_from');
        if (!foundBefore) throw new Error('前置：bob incoming 不含 alice-001 的 derived_from');
        // alice 删除关联
        const del = await api.deleteLink('asset-alice-001', deletedLinkId, KEYS.alice);
        if (del.status !== 200) throw new Error(`删除失败：${del.status}`);
        deletedLinkId = null;
        // bob 立即查 → incoming 不再含 alice-001
        const mid = await api.getLinks('asset-bob-001', KEYS.bob);
        const foundMid = (mid.body.incoming || []).find(i => i.src_asset_id === 'asset-alice-001' && i.link_type === 'derived_from');
        // 重新创建恢复
        const re = await api.createLink('asset-alice-001', 'asset-bob-001', 'derived_from', KEYS.alice);
        if (re.status !== 200) throw new Error(`恢复创建失败：${re.status}`);
        // bob 再查 → incoming 又有
        const after = await api.getLinks('asset-bob-001', KEYS.bob);
        const foundAfter = (after.body.incoming || []).find(i => i.src_asset_id === 'asset-alice-001' && i.link_type === 'derived_from');
        if (!foundMid && foundAfter) {
          record('3.4 关联删除→对端消失', 'PASS', `删除前 bob incoming 有；删除后无；恢复后有`);
        } else {
          throw new Error(`mid found=${!!foundMid}(期望 false), after found=${!!foundAfter}(期望 true)`);
        }
      } catch (e) {
        record('3.4 关联删除→对端消失', 'FAIL', e.message);
      } finally {
        // 兜底：如果删除后未恢复，恢复
        if (deletedLinkId) {
          try { await api.createLink('asset-alice-001', 'asset-bob-001', 'derived_from', KEYS.alice); }
          catch (e) { console.log('  兜底恢复 alice-001→bob-001 失败：', e.message); }
        }
      }
    }

    // ==================================================================
    // 4. 数据一致性测试
    // ==================================================================
    console.log('\n=== 4. 数据一致性测试 ===\n');

    // ---- 4.1 共享库 total 一致（按 scope 访问控制分别验证）----
    console.log('--- 4.1 共享库 total 一致性 ---');
    {
      // 每个用户看到的共享库 total = team + public + 自己的 private + 自己有 ACL 的 restricted
      // 系统 total：
      //   team: alice-003, alice-004, bob-001, bob-003, charlie-001, charlie-003 (6)
      //   public: alice-005, bob-004 (2)
      //   private: alice-002 (1), bob-002 (1), charlie-004 (1)
      //   restricted: alice-001 (ACL: bob read, charlie execute), charlie-002 (no ACL) (2)
      // 期望可见 total：
      //   alice:   6 + 2 + 1(自己 private) + 1(自己 restricted alice-001) = 10
      //   bob:     6 + 2 + 1(自己 private bob-002) + 1(ACL alice-001) = 10
      //   charlie: 6 + 2 + 1(自己 private charlie-004) + 1(自己 restricted charlie-002) + 1(ACL alice-001) = 11
      //   dave:    6 + 2 = 8
      //   eve:     6 + 2 = 8
      const expects = { alice: 10, bob: 10, charlie: 11, dave: 8, eve: 8 };
      const details = [];
      let allPass = true;
      for (const [u, k] of Object.entries(KEYS)) {
        const r = await api.listAssets('?limit=100', k);
        const actual = r.body?.total;
        const pass = actual === expects[u];
        if (!pass) allPass = false;
        details.push(`${u}=${actual}(期望${expects[u]})`);
      }
      record('4.1 共享库 total 一致性', allPass ? 'PASS' : 'FAIL', details.join(', '));
    }

    // ---- 4.2 图谱对称性 ----
    console.log('--- 4.2 图谱对称性（alice-001 ↔ bob-001 ↔ charlie-001）---');
    {
      try {
        // alice 从 alice-001 BFS depth=2 → 应含 bob-001
        const [r1, r2] = await Promise.all([
          api.getGraph('asset-alice-001', 2, KEYS.alice),
          api.getGraph('asset-bob-001', 2, KEYS.bob),
        ]);
        if (r1.status !== 200 || r2.status !== 200) throw new Error(`status: alice=${r1.status}, bob=${r2.status}`);
        const aliceNodes = (r1.body.nodes || []).map(n => n.id);
        const bobNodes = (r2.body.nodes || []).map(n => n.id);
        // alice 视角：alice-001 → bob-001 应有 derived_from 边
        const aliceEdges = r1.body.edges || [];
        const hasAliceToBob = aliceEdges.some(e => e.src === 'asset-alice-001' && e.dst === 'asset-bob-001' && e.link_type === 'derived_from');
        // bob 视角：bob-001 → charlie-001 应有 related_to 边
        const bobEdges = r2.body.edges || [];
        const hasBobToCharl = bobEdges.some(e => e.src === 'asset-bob-001' && e.dst === 'asset-charlie-001' && e.link_type === 'related_to');
        // alice BFS depth=2 应覆盖 bob-001
        const aliceHasBob = aliceNodes.includes('asset-bob-001');
        // bob BFS depth=2 应覆盖 alice-001（反向可达：bob-001 是 alice-001 的目标，反向遍历可到 alice-001）
        const bobHasAlice = bobNodes.includes('asset-alice-001');
        if (hasAliceToBob && hasBobToCharl && aliceHasBob && bobHasAlice) {
          record('4.2 图谱对称性', 'PASS', `alice→bob-001 edge=${hasAliceToBob}, bob→charlie-001 edge=${hasBobToCharl}, aliceHasBob=${aliceHasBob}, bobHasAlice=${bobHasAlice}`);
        } else {
          throw new Error(`aliceToBob=${hasAliceToBob}, bobToCharl=${hasBobToCharl}, aliceHasBob=${aliceHasBob}, bobHasAlice=${bobHasAlice}`);
        }
      } catch (e) {
        record('4.2 图谱对称性', 'FAIL', e.message);
      }
    }

    // ---- 4.3 分页一致性 ----
    console.log('--- 4.3 分页一致性（alice 共享库 limit=2 offset=0/2）---');
    {
      try {
        const [r1, r2] = await Promise.all([
          api.listAssets('?limit=2&offset=0', KEYS.alice),
          api.listAssets('?limit=2&offset=2', KEYS.alice),
        ]);
        if (r1.status !== 200 || r2.status !== 200) throw new Error(`status: page1=${r1.status}, page2=${r2.status}`);
        const ids1 = (r1.body.items || []).map(i => i.id);
        const ids2 = (r2.body.items || []).map(i => i.id);
        const total1 = r1.body.total;
        const total2 = r2.body.total;
        // 不重叠
        const overlap = ids1.filter(id => ids2.includes(id));
        // total 一致
        const totalMatch = total1 === total2;
        if (ids1.length === 2 && ids2.length === 2 && overlap.length === 0 && totalMatch) {
          record('4.3 分页一致性', 'PASS', `page1=[${ids1.join(',')}], page2=[${ids2.join(',')}], total=${total1}`);
        } else {
          throw new Error(`ids1=${ids1.length}项, ids2=${ids2.length}项, 重叠=${overlap.length}, total1=${total1}, total2=${total2}`);
        }
      } catch (e) {
        record('4.3 分页一致性', 'FAIL', e.message);
      }
    }

  } finally {
    // ==================================================================
    // 恢复数据原状
    // ==================================================================
    try { await restoreState(); }
    catch (e) { console.log('恢复数据时出错：', e.message); }

    if (browser) await browser.close();

    // ==================================================================
    // 汇总报告
    // ==================================================================
    console.log('\n================ 多用户测试汇总 ================');
    console.log(`总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}`);
    console.log('------------------------------------------');
    for (const r of results) {
      const tag = r.status === 'PASS' ? '[PASS]' : r.status === 'FAIL' ? '[FAIL]' : '[SKIP]';
      console.log(`${tag} ${r.name}${r.detail ? ' — ' + r.detail : ''}`);
    }
    console.log('==========================================');
    console.log(`截图目录：${SCREEN_DIR}`);

    // 写入结果文件
    const reportPath = path.join(__dirname, 'multi-user-results.txt');
    const reportContent =
      `TeamHarness 多用户并发测试结果\n` +
      `生成时间：${new Date().toISOString()}\n` +
      `测试地址：${BASE}\n` +
      `总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}\n\n` +
      results.map(r => `[${r.status}] ${r.name}${r.detail ? ' — ' + r.detail : ''}`).join('\n') +
      `\n\n截图目录：${SCREEN_DIR}\n`;
    fs.writeFileSync(reportPath, reportContent, 'utf8');
    console.log(`结果文件：${reportPath}`);

    process.exit(failCount > 0 ? 1 : 0);
  }
})();
